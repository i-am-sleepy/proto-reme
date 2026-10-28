#!/usr/bin/env python3
"""GPU time per render pass for DXVK 2.5.3 (Android port).  Apply AFTER dxvk-passlog.py.

  usage:   dxvk-passtime.py <dxvk source dir>
  runtime: with DXVK_PASS_LOG=<N> in env.ini, every pass of one frame in N also gets a GPU time in the DXVK log:
           passgpu f<frame> <ms> ms draws=<n> <opener> <size> <attachment ops>

A bottom-of-pipe timestamp is written before vkCmdBeginRendering and after vkCmdEndRendering; the pair is read back a
few frames later (never blocking). Only logged frames are touched, and the timestamps drain the pipeline there, so use
the numbers to compare passes with each other. Every anchor is asserted; nothing is written unless all of them match.
"""
import pathlib, sys

root = pathlib.Path(sys.argv[1])
_files = {}


def edit(rel, old, new):
    s = _files.get(rel)
    if s is None:
        s = _files[rel] = (root / rel).read_text()
    n = s.count(old)
    assert n == 1, f"{rel}: expected exactly 1 match of {old[:70]!r}, found {n}"
    _files[rel] = s.replace(old, new)


H = "src/dxvk/dxvk_passlog.h"
assert "PassTime" not in (root / H).read_text(), "already applied"

edit(H, "  inline void passLogBegin(const char* caller, const VkRenderingInfo* info) {",
        "  inline std::string passLogBegin(const char* caller, const VkRenderingInfo* info) {")
edit(H, "    Logger::info(s);\n  }\n\n  inline void passLogEnd(",
        "    Logger::info(s);\n    return s;\n  }\n\n  inline void passLogEnd(")
edit(H, '''      suspend ? " (suspend)" : "", " draws=", draws));
  }
''', '''      suspend ? " (suspend)" : "", " draws=", draws));
  }


  // GPU time per render pass (logged frames only): bottom-of-pipe timestamps before vkCmdBeginRendering and after
  // vkCmdEndRendering, read back a few frames later without ever blocking, then logged as "passgpu".
  class PassTime {

  public:

    static uint32_t writeBegin(const Rc<vk::DeviceFn>& vkd, VkCommandBuffer cmd) {
      PassTime& s = instance();
      std::lock_guard<std::mutex> lock(s.mutex);

      if (!s.pool && !s.initPool(vkd))
        return ~0u;

      uint32_t slot = s.next;
      s.next = (s.next + 2) % kSlots;

      vkd->vkResetQueryPool(vkd->device(), s.pool, slot, 2);
      vkd->vkCmdWriteTimestamp(cmd, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, s.pool, slot);
      return slot;
    }

    static void writeEnd(const Rc<vk::DeviceFn>& vkd, VkCommandBuffer cmd, uint32_t slot,
                         std::string desc, uint64_t draws, uint32_t frame) {
      PassTime& s = instance();
      std::lock_guard<std::mutex> lock(s.mutex);

      vkd->vkCmdWriteTimestamp(cmd, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, s.pool, slot + 1);

      if (s.pending.size() < 1024)
        s.pending.push_back({ slot, frame, draws, std::move(desc) });
    }

    static void poll(const Rc<vk::DeviceFn>& vkd, float period) {
      if (!passLogEvery())
        return;

      PassTime& s = instance();
      std::lock_guard<std::mutex> lock(s.mutex);

      if (!s.pool)
        return;

      for (auto it = s.pending.begin(); it != s.pending.end(); ) {
        uint64_t data[4] = { };

        vkd->vkGetQueryPoolResults(vkd->device(), s.pool, it->slot, 2, sizeof(data), data,
          2 * sizeof(uint64_t), VK_QUERY_RESULT_64_BIT | VK_QUERY_RESULT_WITH_AVAILABILITY_BIT);

        if (data[1] && data[3]) {
          double ms = double(data[2] - data[0]) * double(period) / 1.0e6;
          Logger::info(str::format("passgpu f", it->frame, " ", ms, " ms draws=", it->draws, " ", it->desc));
          it = s.pending.erase(it);
        } else if (g_passLogFrame.load() - it->frame > 600) {
          it = s.pending.erase(it);
        } else {
          ++it;
        }
      }
    }

  private:

    struct Record {
      uint32_t    slot;
      uint32_t    frame;
      uint64_t    draws;
      std::string desc;
    };

    static constexpr uint32_t kSlots = 2048;

    std::mutex          mutex;
    VkQueryPool         pool = VK_NULL_HANDLE;
    bool                bad  = false;
    uint32_t            next = 0;
    std::vector<Record> pending;

    static PassTime& instance() {
      static PassTime* p = new PassTime();   // intentionally leaked: no static destructor at process exit
      return *p;
    }

    bool initPool(const Rc<vk::DeviceFn>& vkd) {
      if (bad)
        return false;

      VkQueryPoolCreateInfo info = { VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO };
      info.queryType  = VK_QUERY_TYPE_TIMESTAMP;
      info.queryCount = kSlots;

      if (vkd->vkCreateQueryPool(vkd->device(), &info, nullptr, &pool) != VK_SUCCESS) {
        pool = VK_NULL_HANDLE;
        bad  = true;
        Logger::warn("passgpu: could not create the timestamp query pool, no GPU times");
        return false;
      }

      return true;
    }

  };
''')

edit("src/dxvk/dxvk_cmdlist.h", '''      if (unlikely(passLogOn()))
        passLogBegin(caller, pRenderingInfo);
''', '''      m_passTsSlot = ~0u;

      if (unlikely(passLogOn())) {
        m_passTsDesc = passLogBegin(caller, pRenderingInfo);
        m_passTsSlot = PassTime::writeBegin(m_vkd, getCmdBuffer());
      }
''')
edit("src/dxvk/dxvk_cmdlist.h", '''    void cmdEndRendering() {
      m_vkd->vkCmdEndRendering(getCmdBuffer());
    }
''', '''    void cmdEndRendering() {
      m_vkd->vkCmdEndRendering(getCmdBuffer());

      if (unlikely(m_passTsSlot != ~0u)) {
        PassTime::writeEnd(m_vkd, getCmdBuffer(), m_passTsSlot, std::move(m_passTsDesc),
          drawsSincePassBegin(), g_passLogFrame.load());
        m_passTsSlot = ~0u;
      }
    }
''')
edit("src/dxvk/dxvk_cmdlist.h", "    uint64_t                  m_passDrawBase = 0;\n",
     "    uint64_t                  m_passDrawBase = 0;\n    uint32_t                  m_passTsSlot   = ~0u;\n    std::string               m_passTsDesc;\n")
edit("src/dxvk/dxvk_device.cpp", "    g_passLogFrame.fetch_add(1, std::memory_order_relaxed);\n",
     "    g_passLogFrame.fetch_add(1, std::memory_order_relaxed);\n    PassTime::poll(m_vkd, m_properties.core.properties.limits.timestampPeriod);\n")

for rel, text in _files.items():
    (root / rel).write_text(text)
print("dxvk-passtime: applied (%d files edited)" % len(_files))
