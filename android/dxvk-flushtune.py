#!/usr/bin/env python3
"""Tunable implicit-flush threshold for DXVK 2.5.3 (Android port, tile-based GPUs).

  usage:   dxvk-flushtune.py <dxvk source dir>      (apply AFTER android/patches/dxvk-android.patch)
  runtime: DXVK_FLUSH_HINT_CHUNKS=<n> in env.ini    (unset or 0 = stock DXVK behaviour)

Stock DXVK submits the command buffer as soon as 3 command chunks are queued after any "strong hint" (every EndScene,
every change of render target 0). On a tile-based GPU each submit ends the render pass, so the whole colour, depth and
stencil buffer is written to memory and read back again: the pass log of this game shows ~5 such splits of the full
resolution scene per frame. With this patch, hints only flush once <n> chunks are queued (a whole frame is ~35-50
chunks). Synchronization flushes (blocking locks, queries the game waits on) and the "GPU is about to go idle" rule
are untouched, so busy-waiting games cannot hang. All anchors are asserted; nothing is written unless all match.
"""
import pathlib, sys

root = pathlib.Path(sys.argv[1])
rel = "src/util/util_flush.cpp"
s = (root / rel).read_text()
assert "DXVK_FLUSH_HINT_CHUNKS" not in s, f"{rel}: already patched"


def sub(old, new):
    global s
    n = s.count(old)
    assert n == 1, f"{rel}: expected exactly 1 match of {old[:70]!r}, found {n}"
    s = s.replace(old, new)


sub('#include "util_flush.h"\n', '#include <cstdlib>\n\n#include "util_flush.h"\n')
sub('''    switch (flushType) {
      case GpuFlushType::ExplicitFlush: {''', '''    // Android port (tile-based GPUs): every mid-frame submit ends the render pass and forces a full colour/depth/stencil
    // store and reload. Implicit hints (EndScene, render target changes, ...) only flush once DXVK_FLUSH_HINT_CHUNKS
    // chunks are queued; 0 or unset keeps the stock behaviour. Synchronization flushes are not touched.
    static const uint32_t hintChunks = [] {
      const char* e = std::getenv("DXVK_FLUSH_HINT_CHUNKS");
      return e ? uint32_t(std::atoi(e)) : 0u;
    }();

    if (hintChunks && (flushType == GpuFlushType::ImplicitStrongHint
                    || flushType == GpuFlushType::ImplicitWeakHint)) {
      // Keep the GPU fed if it is about to go idle
      if (uint32_t(m_lastFlushSubmissionId - lastCompleteSubmissionId) < minPendingSubmissions)
        return true;

      return chunkCount >= hintChunks;
    }

    switch (flushType) {
      case GpuFlushType::ExplicitFlush: {''')
(root / rel).write_text(s)
print("dxvk-flushtune: applied")
