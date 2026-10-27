#!/usr/bin/env python3
"""Two additions for DXVK 2.5.3 on the Android port (apply AFTER android/patches/dxvk-android.patch).

  usage: dxvk-extras.py <dxvk source dir>

1. Persistent pipeline cache (on by default).  DXVK passes VK_NULL_HANDLE as pipeline cache at every pipeline creation,
   so compiled shaders only survive a restart if the driver's own disk cache works, which it does not on this setup.
   This creates ONE VkPipelineCache per process, seeds it from <DXVK_STATE_CACHE_PATH>/dxvk_pipeline.cache, writes it
   back every ~20 s when new pipelines were created and on shutdown (atomically, via a .tmp file), and logs what it does
   (path, bytes, errno on failure) to the DXVK log.  DXVK_PIPELINE_CACHE=0 turns it off.
2. DXVK_MAX_ANISO=<n> (env.ini): caps the anisotropy the game requests (it asks for up to 8x).  Unset = unchanged.

Every anchor is asserted and nothing is written unless all of them match.
"""
import pathlib, re, sys

root = pathlib.Path(sys.argv[1])
_files = {}


def load(rel):
    if rel not in _files:
        _files[rel] = (root / rel).read_text()
    return _files[rel]


def edit(rel, old, new):
    s = load(rel)
    n = s.count(old)
    assert n == 1, f"{rel}: expected exactly 1 match of {old[:70]!r}, found {n}"
    _files[rel] = s.replace(old, new)


_header = r'''#pragma once

#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "dxvk_include.h"

namespace dxvk {

  /**
   * One process-wide VkPipelineCache that survives restarts. See android/dxvk-extras.py for the reasoning.
   * Lifetime: created lazily by the first pipeline creation, saved by a background thread, flushed and destroyed by
   * DxvkDevice::~DxvkDevice (workers are stopped and the VkDevice is still alive at that point).
   */
  class PipeCache {

  public:

    // Cache to pass to vkCreate*Pipelines (VK_NULL_HANDLE if disabled or unavailable).
    static VkPipelineCache get(const Rc<vk::DeviceFn>& vkd) {
      PipeCache& s = instance();
      s.created.fetch_add(1, std::memory_order_relaxed);

      VkPipelineCache c = s.cache.load(std::memory_order_acquire);

      if (c != VK_NULL_HANDLE || s.unavailable.load(std::memory_order_relaxed))
        return c;

      return s.init(vkd);
    }

    static void shutdown() {
      instance().stop();
    }

  private:

    std::atomic<VkPipelineCache>  cache       = { VK_NULL_HANDLE };
    std::atomic<bool>             unavailable = { false };
    std::atomic<uint32_t>         created     = { 0 };

    std::mutex                    initMutex;
    std::mutex                    saveMutex;
    std::condition_variable       saveCond;
    bool                          stopFlag    = false;
    std::thread                   saver;

    const vk::DeviceFn*           fn          = nullptr;
    VkDevice                      device      = VK_NULL_HANDLE;
    std::string                   path;
    uint32_t                      savedAt     = 0;

    static PipeCache& instance() {
      static PipeCache* p = new PipeCache();   // intentionally leaked: no static destructor at process exit
      return *p;
    }

    VkPipelineCache init(const Rc<vk::DeviceFn>& vkd) {
      std::lock_guard<std::mutex> lock(initMutex);

      VkPipelineCache c = cache.load();

      if (c != VK_NULL_HANDLE || unavailable.load())
        return c;

      const char* en = std::getenv("DXVK_PIPELINE_CACHE");

      if (en && std::string(en) == "0") {
        Logger::info("pipeline cache: disabled (DXVK_PIPELINE_CACHE=0)");
        unavailable.store(true);
        return VK_NULL_HANDLE;
      }

      const char* dir = std::getenv("DXVK_STATE_CACHE_PATH");
      path = std::string(dir && *dir ? dir : ".") + "/dxvk_pipeline.cache";

      std::vector<char> data;
      {
        std::ifstream f(path, std::ios::binary | std::ios::ate);

        if (f) {
          std::streamsize n = f.tellg();

          if (n > 0) {
            data.resize(size_t(n));
            f.seekg(0);

            if (!f.read(data.data(), n))
              data.clear();
          }
        }
      }

      VkPipelineCacheCreateInfo info = { VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO };
      info.initialDataSize = data.size();
      info.pInitialData    = data.empty() ? nullptr : data.data();

      VkPipelineCache created_cache = VK_NULL_HANDLE;
      VkResult vr = vkd->vkCreatePipelineCache(vkd->device(), &info, nullptr, &created_cache);

      if (vr != VK_SUCCESS && !data.empty()) {
        Logger::warn(str::format("pipeline cache: driver rejected ", data.size(), " bytes from ", path, " (", vr, "), starting empty"));
        info.initialDataSize = 0;
        info.pInitialData    = nullptr;
        vr = vkd->vkCreatePipelineCache(vkd->device(), &info, nullptr, &created_cache);
        data.clear();
      }

      if (vr != VK_SUCCESS) {
        Logger::warn(str::format("pipeline cache: vkCreatePipelineCache failed (", vr, "), running without"));
        unavailable.store(true);
        return VK_NULL_HANDLE;
      }

      Logger::info(str::format("pipeline cache: ", path, ", loaded ", data.size(), " bytes"));

      fn     = vkd.ptr();
      device = vkd->device();
      saver  = std::thread([this] { saveLoop(); });
      cache.store(created_cache, std::memory_order_release);
      return created_cache;
    }

    void saveLoop() {
      std::unique_lock<std::mutex> lock(saveMutex);

      while (!stopFlag) {
        saveCond.wait_for(lock, std::chrono::seconds(20), [this] { return stopFlag; });

        if (stopFlag)
          break;

        lock.unlock();
        saveIfDirty(cache.load(std::memory_order_acquire));
        lock.lock();
      }
    }

    void saveIfDirty(VkPipelineCache c) {
      uint32_t now = created.load(std::memory_order_relaxed);

      if (c == VK_NULL_HANDLE || now == savedAt)
        return;

      size_t size = 0;
      VkResult vr = fn->vkGetPipelineCacheData(device, c, &size, nullptr);

      if (vr != VK_SUCCESS || !size) {
        Logger::warn(str::format("pipeline cache: vkGetPipelineCacheData failed (", vr, ")"));
        return;
      }

      std::vector<char> buf(size);
      vr = fn->vkGetPipelineCacheData(device, c, &size, buf.data());

      if (vr != VK_SUCCESS && vr != VK_INCOMPLETE) {
        Logger::warn(str::format("pipeline cache: vkGetPipelineCacheData failed (", vr, ")"));
        return;
      }

      std::string tmp = path + ".tmp";

      {
        std::ofstream f(tmp, std::ios::binary | std::ios::trunc);

        if (!f) {
          Logger::warn(str::format("pipeline cache: cannot write ", tmp, ": ", std::strerror(errno)));
          return;
        }

        f.write(buf.data(), std::streamsize(size));
        f.flush();

        if (!f) {
          Logger::warn(str::format("pipeline cache: short write to ", tmp, ": ", std::strerror(errno)));
          return;
        }
      }

      if (std::rename(tmp.c_str(), path.c_str()) != 0) {
        Logger::warn(str::format("pipeline cache: cannot rename ", tmp, ": ", std::strerror(errno)));
        return;
      }

      Logger::info(str::format("pipeline cache: saved ", size, " bytes (", now - savedAt, " pipeline creations since last save)"));
      savedAt = now;
    }

    void stop() {
      std::thread t;

      {
        std::lock_guard<std::mutex> lock(saveMutex);
        stopFlag = true;
        t = std::move(saver);
      }

      saveCond.notify_all();

      if (t.joinable())
        t.join();

      unavailable.store(true);                       // nobody gets a handle from now on
      VkPipelineCache c = cache.exchange(VK_NULL_HANDLE);

      if (c != VK_NULL_HANDLE) {
        saveIfDirty(c);
        fn->vkDestroyPipelineCache(device, c, nullptr);
      }
    }

  };

}
'''

# ---- 1. pipeline cache: header, 8 creation sites, shutdown
for rel in ("src/dxvk/dxvk_graphics.cpp", "src/dxvk/dxvk_shader.cpp", "src/dxvk/dxvk_compute.cpp"):
    edit(rel, '#include "dxvk_device.h"\n', '#include "dxvk_device.h"\n#include "dxvk_pipecache.h"\n')

pat = re.compile(r'(vkCreate(?:Graphics|Compute)Pipelines\(\s*vk->device\(\),\s*)VK_NULL_HANDLE')
expected = {"src/dxvk/dxvk_graphics.cpp": 4, "src/dxvk/dxvk_shader.cpp": 3, "src/dxvk/dxvk_compute.cpp": 1}
for rel, want in expected.items():
    s = load(rel)
    s, n = pat.subn(r'\1PipeCache::get(vk)', s)
    assert n == want, f"{rel}: expected {want} pipeline creation sites, found {n}"
    _files[rel] = s

edit("src/dxvk/dxvk_device.cpp", '#include "dxvk_device.h"\n', '#include "dxvk_device.h"\n#include "dxvk_pipecache.h"\n')
edit("src/dxvk/dxvk_device.cpp", "    m_objects.pipelineManager().stopWorkerThreads();\n",
     "    m_objects.pipelineManager().stopWorkerThreads();\n    PipeCache::shutdown();\n")

# ---- 2. anisotropy cap
edit("src/d3d9/d3d9_device.cpp", '#include "d3d9_device.h"\n', '#include <cstdlib>\n\n#include "d3d9_device.h"\n')
edit("src/d3d9/d3d9_device.cpp", '''        key.setAniso(anisotropy);
''', '''        // Android port: DXVK_MAX_ANISO=<n> caps the anisotropy the game requests (up to 8x); unset = unchanged
        static const uint32_t maxAniso = [] {
          const char* e = std::getenv("DXVK_MAX_ANISO");
          return e ? uint32_t(std::atoi(e)) : 0u;
        }();

        if (maxAniso && anisotropy > maxAniso)
          anisotropy = maxAniso;

        key.setAniso(anisotropy);
''')

(root / "src/dxvk/dxvk_pipecache.h").write_text(_header)
for rel, text in _files.items():
    (root / rel).write_text(text)
print("dxvk-extras: applied (%d files edited + dxvk_pipecache.h added)" % len(_files))
