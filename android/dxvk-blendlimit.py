#!/usr/bin/env python3
"""Blended-3D-draw limiter for DXVK 2.5.3 (Android port).  Apply AFTER dxvk-passlog.py (uses its frame counter).

  usage:   dxvk-blendlimit.py <dxvk source dir>
  runtime (env.ini; both unset = nothing changes):
     DXVK_BLEND_SKIP=1       skip EVERY blended, depth-tested draw. A measuring tool: explosions/smoke/fire vanish.
     DXVK_BLEND_BUDGET=<n>   only the first <n> such draws of each frame are drawn; the rest are skipped.
  Draws with depth testing off (the HUD) are never touched. Skipping a draw is safe for the GPU; it only changes the image.
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


H, C = "src/dxvk/dxvk_context.h", "src/dxvk/dxvk_context.cpp"
assert "skipBlendedDraw" not in (root / C).read_text(), "already applied"

edit(H, "    void spillRenderPass(bool suspend, const char* caller = __builtin_FUNCTION());\n",
        "    void spillRenderPass(bool suspend, const char* caller = __builtin_FUNCTION());\n\n"
        "    bool skipBlendedDraw();\n    uint32_t m_blendFrame = 0;\n    uint32_t m_blendCount = 0;\n")

edit(C, "  void DxvkContext::draw(\n          uint32_t vertexCount,\n", '''  // Android port: DXVK_BLEND_SKIP=1 skips every blended, depth-tested draw (3D effects: smoke, fire, glass; HUD draws
  // have depth testing off and are untouched). DXVK_BLEND_BUDGET=<n> lets only the first n of them through per frame.
  // Both unset = nothing changes.
  bool DxvkContext::skipBlendedDraw() {
    static const uint32_t skipAll = [] {
      const char* e = std::getenv("DXVK_BLEND_SKIP");
      return e ? uint32_t(std::atoi(e)) : 0u;
    }();
    static const uint32_t budget = [] {
      const char* e = std::getenv("DXVK_BLEND_BUDGET");
      return e ? uint32_t(std::atoi(e)) : 0u;
    }();

    if (!skipAll && !budget)
      return false;

    if (!m_state.gp.state.omBlend[0].blendEnable() || !m_state.gp.state.ds.enableDepthTest())
      return false;

    if (skipAll)
      return true;

    uint32_t frame = g_passLogFrame.load(std::memory_order_relaxed);

    if (frame != m_blendFrame) {
      m_blendFrame = frame;
      m_blendCount = 0;
    }

    return ++m_blendCount > budget;
  }


  void DxvkContext::draw(
          uint32_t vertexCount,
''')
edit(C, "    if (this->commitGraphicsState<false, false>()) {\n      m_cmd->cmdDraw(\n",
        "    if (!this->skipBlendedDraw() && this->commitGraphicsState<false, false>()) {\n      m_cmd->cmdDraw(\n")
edit(C, "    if (this->commitGraphicsState<true, false>()) {\n      m_cmd->cmdDrawIndexed(\n",
        "    if (!this->skipBlendedDraw() && this->commitGraphicsState<true, false>()) {\n      m_cmd->cmdDrawIndexed(\n")
for rel, text in _files.items():
    (root / rel).write_text(text)
print("dxvk-blendlimit: applied (%d files edited)" % len(_files))
