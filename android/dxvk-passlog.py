#!/usr/bin/env python3
"""Diagnostic patch for DXVK 2.5.3 (Android port): log every Vulkan render pass of one frame in N.

  usage:  dxvk-passlog.py <dxvk source dir>      (apply AFTER android/patches/dxvk-android.patch)
  runtime: set DXVK_PASS_LOG=<N> (e.g. 100) in env.ini; nothing is logged or slowed down without it.

Each logged pass prints which DXVK function opened it, its size, load/store ops, how many draws it held, and which
function ended it. Every anchor is asserted, so a source mismatch fails the build loudly instead of being skipped.
"""
import pathlib, sys

root = pathlib.Path(sys.argv[1])


_files = {}   # all edits are made in memory first; nothing is written unless every anchor matched


def edit(rel, old, new):
    s = _files.get(rel)
    if s is None:
        s = _files[rel] = (root / rel).read_text()
    n = s.count(old)
    assert n == 1, f"{rel}: expected exactly 1 match of {old[:70]!r}, found {n}"
    _files[rel] = s.replace(old, new)


_passlog_h = (r'''#pragma once

#include <atomic>
#include <cstdlib>
#include <string>

#include "dxvk_include.h"

namespace dxvk {

  // Diagnostic only. DXVK_PASS_LOG=<N> logs all render passes of every N-th frame (frames counted at present).
  inline std::atomic<uint32_t> g_passLogFrame{0};

  inline uint32_t passLogEvery() {
    static const uint32_t every = [] {
      const char* e = std::getenv("DXVK_PASS_LOG");
      return e ? uint32_t(std::atoi(e)) : 0u;
    }();
    return every;
  }

  inline bool passLogOn() {
    uint32_t n = passLogEvery();
    return n != 0 && (g_passLogFrame.load(std::memory_order_relaxed) % n) == 0;
  }

  inline const char* passLogLoad(VkAttachmentLoadOp op) {
    return op == VK_ATTACHMENT_LOAD_OP_LOAD ? "L" : (op == VK_ATTACHMENT_LOAD_OP_CLEAR ? "C" : "x");
  }

  inline const char* passLogStore(VkAttachmentStoreOp op) {
    return op == VK_ATTACHMENT_STORE_OP_STORE ? "S" : "x";
  }

  inline void passLogBegin(const char* caller, const VkRenderingInfo* info) {
    std::string s = str::format("passlog f", g_passLogFrame.load(), " begin ", caller, " ",
      info->renderArea.extent.width, "x", info->renderArea.extent.height, " colors=", info->colorAttachmentCount);
    for (uint32_t i = 0; i < info->colorAttachmentCount; i++)
      s += str::format(" c", i, ":", passLogLoad(info->pColorAttachments[i].loadOp), passLogStore(info->pColorAttachments[i].storeOp));
    if (info->pDepthAttachment)
      s += str::format(" depth:", passLogLoad(info->pDepthAttachment->loadOp), passLogStore(info->pDepthAttachment->storeOp));
    if (info->pStencilAttachment)
      s += str::format(" stencil:", passLogLoad(info->pStencilAttachment->loadOp), passLogStore(info->pStencilAttachment->storeOp));
    if (info->flags & VK_RENDERING_RESUMING_BIT)
      s += " resuming";
    if (info->flags & VK_RENDERING_SUSPENDING_BIT)
      s += " suspending";
    Logger::info(s);
  }

  inline void passLogEnd(const char* caller, bool suspend, uint64_t draws) {
    Logger::info(str::format("passlog f", g_passLogFrame.load(), " end   by ", caller,
      suspend ? " (suspend)" : "", " draws=", draws));
  }

}
''')

# --- command list: name the function that opens each pass, count draws inside it
edit("src/dxvk/dxvk_cmdlist.h", '#include "dxvk_graphics.h"\n', '#include "dxvk_graphics.h"\n#include "dxvk_passlog.h"\n')
edit("src/dxvk/dxvk_cmdlist.h", '''    void cmdBeginRendering(
      const VkRenderingInfo*        pRenderingInfo) {
      m_cmd.execCommands = true;
      m_statCounters.addCtr(DxvkStatCounter::CmdRenderPassCount, 1);
''', '''    void cmdBeginRendering(
      const VkRenderingInfo*        pRenderingInfo,
      const char*                   caller = __builtin_FUNCTION()) {
      m_cmd.execCommands = true;
      m_statCounters.addCtr(DxvkStatCounter::CmdRenderPassCount, 1);
      m_passDrawBase = m_statCounters.getCtr(DxvkStatCounter::CmdDrawCalls);

      if (unlikely(passLogOn()))
        passLogBegin(caller, pRenderingInfo);
''')
edit("src/dxvk/dxvk_cmdlist.h", '''    DxvkStatCounters& statCounters() {
      return m_statCounters;
    }
''', '''    DxvkStatCounters& statCounters() {
      return m_statCounters;
    }

    uint64_t drawsSincePassBegin() const {
      return m_statCounters.getCtr(DxvkStatCounter::CmdDrawCalls) - m_passDrawBase;
    }
''')
edit("src/dxvk/dxvk_cmdlist.h", "    DxvkStatCounters          m_statCounters;\n",
     "    DxvkStatCounters          m_statCounters;\n    uint64_t                  m_passDrawBase = 0;\n")

# --- context: name the function that ends each pass
edit("src/dxvk/dxvk_context.h", "    void spillRenderPass(bool suspend);\n",
     "    void spillRenderPass(bool suspend, const char* caller = __builtin_FUNCTION());\n")
edit("src/dxvk/dxvk_context.cpp", '''  void DxvkContext::spillRenderPass(bool suspend) {
    if (m_flags.test(DxvkContextFlag::GpRenderPassBound)) {
      m_flags.clr(DxvkContextFlag::GpRenderPassBound);
''', '''  void DxvkContext::spillRenderPass(bool suspend, const char* caller) {
    if (m_flags.test(DxvkContextFlag::GpRenderPassBound)) {
      m_flags.clr(DxvkContextFlag::GpRenderPassBound);

      if (unlikely(passLogOn()))
        passLogEnd(caller, suspend, m_cmd->drawsSincePassBegin());
''')

# --- device: frame counter (one tick per present)
edit("src/dxvk/dxvk_device.cpp", "    m_submissionQueue.present(presentInfo, status);\n",
     "    m_submissionQueue.present(presentInfo, status);\n    g_passLogFrame.fetch_add(1, std::memory_order_relaxed);\n")
(root / "src/dxvk/dxvk_passlog.h").write_text(_passlog_h)
for rel, text in _files.items():
    (root / rel).write_text(text)
print("dxvk-passlog: applied (%d files edited + dxvk_passlog.h added)" % len(_files))
