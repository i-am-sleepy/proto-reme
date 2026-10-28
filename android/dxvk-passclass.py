#!/usr/bin/env python3
"""Per-pass draw classification for DXVK 2.5.3 (Android port).  Apply AFTER dxvk-passtime.py.

  usage:   dxvk-passclass.py <dxvk source dir>
  runtime: with DXVK_PASS_LOG=<N>, every "passgpu" line also ends with   blend=<b> depthwrite=<w> nodepthtest=<n>
           = how many of the pass's draws had colour blending on, wrote depth, or had depth testing off.
           Lets us tell large blended effects (particles, smoke, glass) from opaque geometry inside the heavy pass.
Every anchor is asserted; nothing is written unless all of them match.
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


C = "src/dxvk/dxvk_cmdlist.h"
assert "m_passNBlend" not in (root / C).read_text(), "already applied"

edit(C, "      m_passTsSlot = ~0u;\n\n      if (unlikely(passLogOn())) {\n",
        "      m_passTsSlot = ~0u;\n      m_passNBlend = m_passNDepthWrite = m_passNNoDepth = 0;\n\n      if (unlikely(passLogOn())) {\n")
edit(C, "      if (unlikely(m_passTsSlot != ~0u)) {\n        PassTime::writeEnd(",
        '      if (unlikely(m_passTsSlot != ~0u)) {\n        m_passTsDesc += str::format(" blend=", m_passNBlend, " depthwrite=", m_passNDepthWrite,\n          " nodepthtest=", m_passNNoDepth);\n        PassTime::writeEnd(')
edit(C, '''    uint64_t drawsSincePassBegin() const {
      return m_statCounters.getCtr(DxvkStatCounter::CmdDrawCalls) - m_passDrawBase;
    }
''', '''    uint64_t drawsSincePassBegin() const {
      return m_statCounters.getCtr(DxvkStatCounter::CmdDrawCalls) - m_passDrawBase;
    }

    void passNoteDraw(bool blend, bool depthWrite, bool depthTest) {
      m_passNBlend      += uint32_t(blend);
      m_passNDepthWrite += uint32_t(depthWrite);
      m_passNNoDepth    += uint32_t(!depthTest);
    }
''')
edit(C, "    std::string               m_passTsDesc;\n",
        "    std::string               m_passTsDesc;\n    uint32_t                  m_passNBlend      = 0;\n    uint32_t                  m_passNDepthWrite = 0;\n    uint32_t                  m_passNNoDepth    = 0;\n")
edit("src/dxvk/dxvk_context.cpp", '''    if (m_flags.test(DxvkContextFlag::DirtyDrawBuffer) && Indirect)
      this->trackDrawBuffer();

    return true;
  }
''', '''    if (m_flags.test(DxvkContextFlag::DirtyDrawBuffer) && Indirect)
      this->trackDrawBuffer();

    if (unlikely(passLogOn()))
      m_cmd->passNoteDraw(m_state.gp.state.omBlend[0].blendEnable(),
                          m_state.gp.state.ds.enableDepthWrite(),
                          m_state.gp.state.ds.enableDepthTest());

    return true;
  }
''')
for rel, text in _files.items():
    (root / rel).write_text(text)
print("dxvk-passclass: applied (%d files edited)" % len(_files))
