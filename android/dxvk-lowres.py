#!/usr/bin/env python3
"""Low-resolution rendering of blended 3D effects for DXVK 2.5.3 (Android port).
Apply AFTER dxvk-passlog.py and dxvk-blendlimit.py (it reuses their frame counter and draw hooks).

  usage:   dxvk-lowres.py <dxvk source dir>
  runtime (env.ini; unset = nothing changes):
     DXVK_LOWRES=1         draw blended, depth-tested, depth-write-off draws (smoke, fire, explosions, glass) into a
                           half-size colour buffer against a half-size copy of the scene depth, then composite that
                           buffer over the scene. About a quarter of the pixels are shaded for those draws.
     DXVK_LOWRES_MIN=<n>   only start doing it in a frame when the previous frame had at least <n> such draws
                           (default 40), so light frames do not pay for the extra depth copy and composite pass.
     DXVK_LOWRES_LOG=1     every ~120 frames, log how many draws were eligible / redirected and how many composites ran.

How it works (everything stays inside DxvkContext, the D3D9 layer is not touched):
  * an eligible draw switches the context to the half-size targets (the game's own targets are remembered),
    scales viewport + scissor, and converts the blend mode to a premultiplied equivalent (alpha, premultiplied and
    additive blends are supported; anything else is drawn normally at full size);
  * the game re-binding its render targets (D3D9 does that whenever depth-write and similar state toggles) is
    intercepted: same images -> only remembered, different images -> the half-size pass is composited first;
  * any operation that ends the render pass (clears, copies, blits, compute, submit, present) or the first
    non-eligible draw leaves the mode: the half-size pass ends, the game's targets are restored and the half-size
    buffer is blended over the scene with DXVK's own blit pass (premultiplied "over", alpha of the scene kept).
  * if the depth copy cannot be done by the hardware blit path the feature disables itself (logged once).
Every anchor is asserted; nothing is written unless all of them match.
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


def redit(rel, pattern, repl):
    s = load(rel)
    s, n = re.subn(pattern, repl, s, count=0, flags=re.S)
    assert n == 1, f"{rel}: expected exactly 1 match of regex {pattern[:70]!r}, found {n}"
    _files[rel] = s


CTX_H, CTX_C = "src/dxvk/dxvk_context.h", "src/dxvk/dxvk_context.cpp"
MB_H, MB_C = "src/dxvk/dxvk_meta_blit.h", "src/dxvk/dxvk_meta_blit.cpp"
assert "lowresBeginDraw" not in load(CTX_C), "already applied"
assert "skipBlendedDraw" in load(CTX_C), "apply dxvk-blendlimit.py first"

# ------------------------------------------------------------------------------------------ blit: blended pipeline
redit(MB_H, r"(    DxvkMetaBlitPipeline getPipeline\(\s*VkImageViewType\s+viewType,\s*VkFormat\s+viewFormat,\s*VkSampleCountFlagBits samples\);)",
      r"""\1

    // Android port: same pipeline, but blends the source over the destination (premultiplied "over", dst alpha kept).
    DxvkMetaBlitPipeline getBlendedPipeline(
            VkImageViewType       viewType,
            VkFormat              viewFormat,
            VkSampleCountFlagBits samples);""")
edit(MB_H, "    dxvk::mutex m_mutex;\n", """    dxvk::mutex m_mutex;

    bool m_createBlended = false;

    std::unordered_map<
      DxvkMetaBlitPipelineKey,
      DxvkMetaBlitPipeline,
      DxvkHash, DxvkEq> m_blendedPipelines;
""")
edit(MB_C, "  VkShaderModule DxvkMetaBlitObjects::createShaderModule(", """  DxvkMetaBlitPipeline DxvkMetaBlitObjects::getBlendedPipeline(
          VkImageViewType       viewType,
          VkFormat              viewFormat,
          VkSampleCountFlagBits samples) {
    std::lock_guard<dxvk::mutex> lock(m_mutex);

    DxvkMetaBlitPipelineKey key;
    key.viewType   = viewType;
    key.viewFormat = viewFormat;
    key.samples    = samples;

    auto entry = m_blendedPipelines.find(key);
    if (entry != m_blendedPipelines.end())
      return entry->second;

    DxvkMetaBlitPipeline pipeline;

    m_createBlended = true;

    try {
      pipeline = this->createPipeline(key);
    } catch (...) {
      m_createBlended = false;
      throw;
    }

    m_createBlended = false;
    m_blendedPipelines.insert({ key, pipeline });
    return pipeline;
  }


  VkShaderModule DxvkMetaBlitObjects::createShaderModule(""")
edit(MB_C, "    VkPipelineColorBlendAttachmentState cbAttachment = { };\n", """    VkPipelineColorBlendAttachmentState cbAttachment = { };

    if (m_createBlended) {
      cbAttachment.blendEnable         = VK_TRUE;
      cbAttachment.srcColorBlendFactor = VK_BLEND_FACTOR_ONE;
      cbAttachment.dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
      cbAttachment.colorBlendOp        = VK_BLEND_OP_ADD;
      cbAttachment.srcAlphaBlendFactor = VK_BLEND_FACTOR_ZERO;
      cbAttachment.dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
      cbAttachment.alphaBlendOp        = VK_BLEND_OP_ADD;
    }
""")

# ------------------------------------------------------------------------------------------ context header
edit(CTX_H, "    bool skipBlendedDraw();\n    uint32_t m_blendFrame = 0;\n    uint32_t m_blendCount = 0;\n", """    bool skipBlendedDraw();
    uint32_t m_blendFrame = 0;
    uint32_t m_blendCount = 0;

    // Android port: low-resolution blended effects (see dxvk-lowres.py)
    bool lowresBeginDraw();
    void lowresEndDraw();
    bool lowresEnter();
    void lowresLeave();
    bool lowresPrepare(const Rc<DxvkImageView>& color, const Rc<DxvkImageView>& depth);
    bool lowresInterceptBind(DxvkRenderTargets& targets, VkImageAspectFlags feedbackLoop);

    bool                  m_lowresActive        = false;
    bool                  m_lowresBusy          = false;
    bool                  m_lowresDisabled      = false;
    bool                  m_blitPremulBlend     = false;
    uint32_t              m_lowresFrame         = ~0u;
    uint32_t              m_lowresCountFrame    = ~0u;
    uint32_t              m_lowresCount         = 0;
    uint32_t              m_lowresPrevCount     = 0;
    uint32_t              m_lowresRedirected    = 0;
    uint32_t              m_lowresComposites    = 0;
    VkImageAspectFlags    m_lowresSavedFeedback = 0;
    DxvkRenderTargets     m_lowresSaved;
    DxvkOmAttachmentBlend m_lowresSavedBlend;
    VkViewport            m_lowresSavedVp       = { };
    VkRect2D              m_lowresSavedSc       = { };
    Rc<DxvkImageView>     m_lowresColor;
    Rc<DxvkImageView>     m_lowresDepth;
""")
edit(CTX_H, "      // Set up default render pass ops and normalize layouts\n      m_state.om.renderTargets = std::move(targets);\n",
            "      // Android port: while rendering effects at half size the game's re-binds are only remembered\n"
            "      if (unlikely(m_lowresActive) && !m_lowresBusy && this->lowresInterceptBind(targets, feedbackLoop))\n"
            "        return;\n\n"
            "      // Set up default render pass ops and normalize layouts\n      m_state.om.renderTargets = std::move(targets);\n")

# ------------------------------------------------------------------------------------------ context: hooks
edit(CTX_C, """    DxvkMetaBlitPipeline pipeInfo = m_common->metaBlit().getPipeline(
      dstView->info().viewType, dstView->info().format,
      dstView->image()->info().sampleCount);
""", """    DxvkMetaBlitPipeline pipeInfo = m_blitPremulBlend
      ? m_common->metaBlit().getBlendedPipeline(
          dstView->info().viewType, dstView->info().format,
          dstView->image()->info().sampleCount)
      : m_common->metaBlit().getPipeline(
          dstView->info().viewType, dstView->info().format,
          dstView->image()->info().sampleCount);
""")
edit(CTX_C, "  void DxvkContext::spillRenderPass(bool suspend, const char* caller) {\n",
            "  void DxvkContext::spillRenderPass(bool suspend, const char* caller) {\n"
            "    if (unlikely(m_lowresActive) && !m_lowresBusy)\n      this->lowresLeave();\n\n")
edit(CTX_C, "  void DxvkContext::invalidateState() {\n",
            "  void DxvkContext::invalidateState() {\n"
            "    if (unlikely(m_lowresActive) && !m_lowresBusy)\n      this->lowresLeave();\n\n")
edit(CTX_C, """    if (!this->skipBlendedDraw() && this->commitGraphicsState<false, false>()) {
      m_cmd->cmdDraw(
        vertexCount, instanceCount,
        firstVertex, firstInstance);
    }
  }
""", """    const bool lowres = this->lowresBeginDraw();

    if (!this->skipBlendedDraw() && this->commitGraphicsState<false, false>()) {
      m_cmd->cmdDraw(
        vertexCount, instanceCount,
        firstVertex, firstInstance);
    }

    if (lowres)
      this->lowresEndDraw();
  }
""")
edit(CTX_C, """    if (!this->skipBlendedDraw() && this->commitGraphicsState<true, false>()) {
      m_cmd->cmdDrawIndexed(
        indexCount, instanceCount,
        firstIndex, vertexOffset,
        firstInstance);
    }
  }
""", """    const bool lowres = this->lowresBeginDraw();

    if (!this->skipBlendedDraw() && this->commitGraphicsState<true, false>()) {
      m_cmd->cmdDrawIndexed(
        indexCount, instanceCount,
        firstIndex, vertexOffset,
        firstInstance);
    }

    if (lowres)
      this->lowresEndDraw();
  }
""")

# ------------------------------------------------------------------------------------------ context: implementation
edit(CTX_C, "  bool DxvkContext::skipBlendedDraw() {", r'''  // ===== Android port: low-resolution blended effects ===========================================================
  // See dxvk-lowres.py for the design. Everything below only runs when DXVK_LOWRES=1.

  static bool lowresEnabled() {
    static const bool v = [] {
      const char* e = std::getenv("DXVK_LOWRES");
      return e && std::atoi(e) != 0;
    }();
    return v;
  }

  static uint32_t lowresMinDraws() {
    static const uint32_t v = [] {
      const char* e = std::getenv("DXVK_LOWRES_MIN");
      return e ? uint32_t(std::atoi(e)) : 40u;
    }();
    return v;
  }

  static bool lowresLogging() {
    static const bool v = [] {
      const char* e = std::getenv("DXVK_LOWRES_LOG");
      return e && std::atoi(e) != 0;
    }();
    return v;
  }

  static bool lowresColorFormatOk(VkFormat f) {
    switch (f) {
      case VK_FORMAT_B8G8R8A8_UNORM:
      case VK_FORMAT_R8G8B8A8_UNORM:
      case VK_FORMAT_A8B8G8R8_UNORM_PACK32:
      case VK_FORMAT_A2B10G10R10_UNORM_PACK32:
      case VK_FORMAT_A2R10G10B10_UNORM_PACK32:
      case VK_FORMAT_R16G16B16A16_UNORM:
      case VK_FORMAT_R16G16B16A16_SFLOAT:
        return true;
      default:
        return false;
    }
  }

  // Converts the game's blend mode into the equivalent for a transparent, premultiplied buffer.
  static bool lowresConvertBlend(const DxvkOmAttachmentBlend& in, DxvkOmAttachmentBlend& out) {
    if (in.colorBlendOp() != VK_BLEND_OP_ADD)
      return false;

    const VkBlendFactor s = in.srcColorBlendFactor();
    const VkBlendFactor d = in.dstColorBlendFactor();
    const VkColorComponentFlags all = VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT
                                    | VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT;

    if ((s == VK_BLEND_FACTOR_SRC_ALPHA || s == VK_BLEND_FACTOR_ONE) && d == VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA) {
      // alpha or premultiplied blending: accumulate premultiplied colour and coverage
      out = DxvkOmAttachmentBlend(VK_TRUE, s, d, VK_BLEND_OP_ADD,
        VK_BLEND_FACTOR_ONE, VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA, VK_BLEND_OP_ADD, all);
      return true;
    }

    if ((s == VK_BLEND_FACTOR_SRC_ALPHA || s == VK_BLEND_FACTOR_ONE) && d == VK_BLEND_FACTOR_ONE) {
      // additive: adds colour, leaves coverage alone
      out = DxvkOmAttachmentBlend(VK_TRUE, s, d, VK_BLEND_OP_ADD,
        VK_BLEND_FACTOR_ZERO, VK_BLEND_FACTOR_ONE, VK_BLEND_OP_ADD, all);
      return true;
    }

    return false;
  }


  bool DxvkContext::lowresPrepare(const Rc<DxvkImageView>& color, const Rc<DxvkImageView>& depth) {
    const DxvkImageCreateInfo& ci = color->image()->info();
    const DxvkImageCreateInfo& di = depth->image()->info();

    // The half-size depth copy has to go through vkCmdBlitImage: a shader blit cannot write depth.
    auto features = m_device->adapter()->getFormatFeatures(di.format);
    auto bits = di.tiling == VK_IMAGE_TILING_OPTIMAL ? features.optimal : features.linear;

    if (!(bits & VK_FORMAT_FEATURE_2_BLIT_SRC_BIT) || !(bits & VK_FORMAT_FEATURE_2_BLIT_DST_BIT)
     || (depth->info().usage & VK_IMAGE_USAGE_SAMPLED_BIT)) {
      Logger::warn("lowres: depth copy needs the hardware blit path, which this depth buffer cannot use; disabled");
      return false;
    }

    VkExtent3D extent = { (ci.extent.width + 1u) / 2u, (ci.extent.height + 1u) / 2u, 1u };

    if (m_lowresColor != nullptr && m_lowresDepth != nullptr
     && m_lowresColor->image()->info().format == ci.format
     && m_lowresDepth->image()->info().format == di.format
     && m_lowresColor->image()->info().extent.width  == extent.width
     && m_lowresColor->image()->info().extent.height == extent.height)
      return true;

    m_lowresColor = nullptr;
    m_lowresDepth = nullptr;

    DxvkImageCreateInfo cinfo = { };
    cinfo.type        = VK_IMAGE_TYPE_2D;
    cinfo.format      = ci.format;
    cinfo.sampleCount = VK_SAMPLE_COUNT_1_BIT;
    cinfo.extent      = extent;
    cinfo.numLayers   = 1u;
    cinfo.mipLevels   = 1u;
    cinfo.usage       = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT;
    cinfo.stages      = VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT | VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT;
    cinfo.access      = VK_ACCESS_COLOR_ATTACHMENT_READ_BIT | VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT | VK_ACCESS_SHADER_READ_BIT;
    cinfo.tiling      = VK_IMAGE_TILING_OPTIMAL;
    cinfo.layout      = VK_IMAGE_LAYOUT_GENERAL;
    cinfo.debugName   = "Half-size effects colour";

    DxvkImageCreateInfo dinfo = { };
    dinfo.type        = VK_IMAGE_TYPE_2D;
    dinfo.format      = di.format;
    dinfo.sampleCount = VK_SAMPLE_COUNT_1_BIT;
    dinfo.extent      = extent;
    dinfo.numLayers   = 1u;
    dinfo.mipLevels   = 1u;
    dinfo.usage       = VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    dinfo.stages      = VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT | VK_PIPELINE_STAGE_LATE_FRAGMENT_TESTS_BIT
                      | VK_PIPELINE_STAGE_TRANSFER_BIT;
    dinfo.access      = VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_READ_BIT | VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT
                      | VK_ACCESS_TRANSFER_WRITE_BIT;
    dinfo.tiling      = VK_IMAGE_TILING_OPTIMAL;
    dinfo.layout      = VK_IMAGE_LAYOUT_GENERAL;
    dinfo.debugName   = "Half-size effects depth";

    Rc<DxvkImage> cimg = m_device->createImage(cinfo, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    Rc<DxvkImage> dimg = m_device->createImage(dinfo, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);

    DxvkImageViewKey cview = { };
    cview.viewType   = VK_IMAGE_VIEW_TYPE_2D;
    cview.usage      = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT;
    cview.format     = ci.format;
    cview.aspects    = VK_IMAGE_ASPECT_COLOR_BIT;
    cview.mipIndex   = 0u;
    cview.mipCount   = 1u;
    cview.layerIndex = 0u;
    cview.layerCount = 1u;

    DxvkImageViewKey dview = { };
    dview.viewType   = VK_IMAGE_VIEW_TYPE_2D;
    dview.usage      = VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT;
    dview.format     = di.format;
    dview.aspects    = lookupFormatInfo(di.format)->aspectMask;
    dview.mipIndex   = 0u;
    dview.mipCount   = 1u;
    dview.layerIndex = 0u;
    dview.layerCount = 1u;

    m_lowresColor = cimg->createView(cview);
    m_lowresDepth = dimg->createView(dview);

    // Put both images into their default layout, like DXVK's own initializer does
    this->initImage(cimg, cimg->getAvailableSubresources(), VK_IMAGE_LAYOUT_UNDEFINED);
    this->initImage(dimg, dimg->getAvailableSubresources(), VK_IMAGE_LAYOUT_UNDEFINED);

    Logger::info(str::format("lowres: half-size effects buffers ", extent.width, "x", extent.height));
    return true;
  }


  bool DxvkContext::lowresEnter() {
    const DxvkAttachment& c = m_state.om.renderTargets.color[0];
    const DxvkAttachment& d = m_state.om.renderTargets.depth;

    if (!this->lowresPrepare(c.view, d.view)) {
      m_lowresDisabled = true;
      return false;
    }

    m_lowresBusy = true;

    const uint32_t frame = g_passLogFrame.load(std::memory_order_relaxed);
    const VkExtent3D full = c.view->image()->info().extent;
    const VkExtent3D half = m_lowresColor->image()->info().extent;

    // Half-size copy of the scene depth (nearest sample), so the effects are still hidden behind geometry
    VkOffset3D srcOffsets[2] = { { 0, 0, 0 }, { int32_t(full.width), int32_t(full.height), 1 } };
    VkOffset3D dstOffsets[2] = { { 0, 0, 0 }, { int32_t(half.width), int32_t(half.height), 1 } };
    this->blitImageView(m_lowresDepth, dstOffsets, d.view, srcOffsets, VK_FILTER_NEAREST);

    m_lowresSaved         = m_state.om.renderTargets;
    m_lowresSavedFeedback = m_state.gp.state.om.feedbackLoop();

    DxvkRenderTargets targets;
    targets.color[0].view   = m_lowresColor;
    targets.color[0].layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    targets.depth.view      = m_lowresDepth;
    targets.depth.layout    = VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL;
    this->bindRenderTargets(std::move(targets), 0);

    if (m_lowresFrame != frame) {
      VkClearValue clear = { };
      this->clearRenderTarget(m_lowresColor, VK_IMAGE_ASPECT_COLOR_BIT, clear);
      m_lowresFrame = frame;
    }

    m_lowresActive = true;
    m_lowresBusy   = false;
    return true;
  }


  void DxvkContext::lowresLeave() {
    if (!m_lowresActive)
      return;

    m_lowresBusy   = true;
    m_lowresActive = false;

    // End the half-size pass, then give the game its own targets back
    this->spillRenderPass(false);

    DxvkRenderTargets saved = m_lowresSaved;
    this->bindRenderTargets(std::move(saved), m_lowresSavedFeedback);

    // Blend the half-size buffer over the scene with DXVK's own blit pass
    const Rc<DxvkImageView> scene = m_lowresSaved.color[0].view;

    if (scene != nullptr && m_lowresColor != nullptr) {
      const VkExtent3D dstExt = scene->mipLevelExtent(0);
      const VkExtent3D srcExt = m_lowresColor->mipLevelExtent(0);

      VkOffset3D dstOffsets[2] = { { 0, 0, 0 }, { int32_t(dstExt.width), int32_t(dstExt.height), 1 } };
      VkOffset3D srcOffsets[2] = { { 0, 0, 0 }, { int32_t(srcExt.width), int32_t(srcExt.height), 1 } };

      this->prepareImage(scene->image(), scene->imageSubresources());
      this->prepareImage(m_lowresColor->image(), m_lowresColor->imageSubresources());

      m_blitPremulBlend = true;
      this->blitImageFb(scene, dstOffsets, m_lowresColor, srcOffsets, VK_FILTER_LINEAR);
      m_blitPremulBlend = false;

      m_lowresComposites++;
    }

    m_lowresSaved = DxvkRenderTargets();
    m_lowresBusy  = false;
  }


  bool DxvkContext::lowresInterceptBind(DxvkRenderTargets& targets, VkImageAspectFlags feedbackLoop) {
    auto sameImage = [] (const Rc<DxvkImageView>& a, const Rc<DxvkImageView>& b) {
      if (a == nullptr || b == nullptr)
        return a == nullptr && b == nullptr;

      return a->image() == b->image();
    };

    bool same = sameImage(targets.depth.view, m_lowresSaved.depth.view);

    for (uint32_t i = 0; i < MaxNumRenderTargets; i++)
      same = same && sameImage(targets.color[i].view, m_lowresSaved.color[i].view);

    if (!same) {
      // The game switches to different render targets: finish the effects on the old ones first
      this->lowresLeave();
      return false;
    }

    // Same images: remember the game's latest bindings (layouts, views, feedback loop), keep drawing at half size
    for (uint32_t i = 0; i < MaxNumRenderTargets; i++) {
      if (targets.color[i].view != nullptr)
        targets.color[i].layout = targets.color[i].view->pickLayout(targets.color[i].layout);
    }

    m_lowresSaved         = targets;
    m_lowresSavedFeedback = feedbackLoop;
    return true;
  }


  bool DxvkContext::lowresBeginDraw() {
    if (!lowresEnabled() || m_lowresDisabled || m_lowresBusy)
      return false;

    const uint32_t frame = g_passLogFrame.load(std::memory_order_relaxed);

    if (frame != m_lowresCountFrame) {
      if (lowresLogging() && m_lowresCountFrame != ~0u && (m_lowresCountFrame % 120u) == 0u)
        Logger::info(str::format("lowres: frame ", m_lowresCountFrame, ": ", m_lowresCount, " eligible draws, ",
          m_lowresRedirected, " redirected, ", m_lowresComposites, " composites (previous frame had ", m_lowresPrevCount, ")"));

      m_lowresPrevCount  = m_lowresCount;
      m_lowresCount      = 0u;
      m_lowresRedirected = 0u;
      m_lowresComposites = 0u;
      m_lowresCountFrame = frame;
    }

    const DxvkRenderTargets& app = m_lowresActive ? m_lowresSaved : m_state.om.renderTargets;
    const DxvkOmAttachmentBlend& blend = m_state.gp.state.omBlend[0];

    DxvkOmAttachmentBlend converted;
    bool eligible = false;

    if (app.color[0].view != nullptr && app.depth.view != nullptr
     && blend.blendEnable()
     && m_state.gp.state.ds.enableDepthTest()
     && !m_state.gp.state.ds.enableDepthWrite()
     && !m_state.gp.state.ds.enableStencilTest()
     && m_state.vp.viewportCount == 1u) {
      bool oneTarget = true;

      for (uint32_t i = 1; i < MaxNumRenderTargets; i++)
        oneTarget = oneTarget && app.color[i].view == nullptr;

      const DxvkImageCreateInfo& ci = app.color[0].view->image()->info();

      eligible = oneTarget
              && app.color[0].view->info().format == ci.format
              && ci.sampleCount == VK_SAMPLE_COUNT_1_BIT
              && ci.extent.width >= 640u && ci.extent.height >= 360u
              && lowresColorFormatOk(ci.format)
              && lowresConvertBlend(blend, converted);
    }

    if (eligible)
      m_lowresCount++;

    // Only worth the extra depth copy and composite when the previous frame had plenty of such draws
    const bool want = eligible && (m_lowresActive || m_lowresPrevCount >= lowresMinDraws());

    if (!want) {
      if (m_lowresActive)
        this->lowresLeave();

      return false;
    }

    if (!m_lowresActive && !this->lowresEnter())
      return false;

    // Per-draw overrides (restored by lowresEndDraw): half-size viewport/scissor and the converted blend mode
    m_lowresSavedBlend = blend;
    m_lowresSavedVp    = m_state.vp.viewports[0];
    m_lowresSavedSc    = m_state.vp.scissorRects[0];

    VkViewport vp = m_lowresSavedVp;
    vp.x      *= 0.5f;
    vp.y      *= 0.5f;
    vp.width  *= 0.5f;
    vp.height *= 0.5f;

    VkRect2D sc = m_lowresSavedSc;
    int32_t x1 = sc.offset.x + int32_t(sc.extent.width);
    int32_t y1 = sc.offset.y + int32_t(sc.extent.height);
    sc.offset.x = sc.offset.x >= 0 ? sc.offset.x / 2 : 0;
    sc.offset.y = sc.offset.y >= 0 ? sc.offset.y / 2 : 0;
    sc.extent.width  = uint32_t(std::max<int32_t>((x1 + 1) / 2 - sc.offset.x, 0));
    sc.extent.height = uint32_t(std::max<int32_t>((y1 + 1) / 2 - sc.offset.y, 0));

    m_state.gp.state.omBlend[0] = converted;
    m_state.vp.viewports[0]     = vp;
    m_state.vp.scissorRects[0]  = sc;
    m_flags.set(DxvkContextFlag::GpDirtyPipelineState, DxvkContextFlag::GpDirtyViewport);

    m_lowresRedirected++;
    return true;
  }


  void DxvkContext::lowresEndDraw() {
    m_state.gp.state.omBlend[0] = m_lowresSavedBlend;
    m_state.vp.viewports[0]     = m_lowresSavedVp;
    m_state.vp.scissorRects[0]  = m_lowresSavedSc;
    m_flags.set(DxvkContextFlag::GpDirtyPipelineState, DxvkContextFlag::GpDirtyViewport);
  }


  bool DxvkContext::skipBlendedDraw() {''')

for rel, text in _files.items():
    (root / rel).write_text(text)
print("dxvk-lowres: applied (%d files edited)" % len(_files))
