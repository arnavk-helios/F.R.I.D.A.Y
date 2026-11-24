from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class AssistantState(str, Enum):
    HARD_OFF = "hard_off"
    ACTIVE = "active"
    LISTENING = "listening"
    PROCESSING = "processing"


@dataclass
class HUDViewModel:
    state: AssistantState = AssistantState.HARD_OFF
    headline: str = "Vayu: Mic Hard Off"
    detail: str = "Audio stream torn down"
    visible: bool = False


def _run_on_main_thread(fn: Callable[[], None]) -> None:
    """Safely marshals Cocoa UI updates onto the macOS main runloop."""
    try:
        from AppKit import NSThread
        from PyObjCTools import AppHelper

        if NSThread.isMainThread():
            fn()
        else:
            AppHelper.callAfter(fn)
    except Exception:
        fn()


# 26x26 pixel-art grid matching Vayu's golden cat-eared robot sphere
# Palette keys:
#   O = dark outline, H = pale gold highlight, G = warm gold, S = amber shadow,
#   M = mint green accent, V = dark visor/port, C = cyan eyes/mouth
_EAR_GRID: list[str] = [
    ".....OO...................",
    "....OHHO..........OO......",
    "...OHHHSO........OHGO.....",
    "..OHGGGSSO......OHGGSO....",
    "..OHGGGSSO.....OHGGGSSO...",
    "..OMGGSSOO.....OHGGGSSO...",
    "...OOOOO........OGGSSOO...",
    ".................OOOOO....",
]

_SPHERE_GRID: list[str] = [
    "........OOOOOOOO..........",
    "......OOHHHHHHHGOO........",
    ".....OHHHHHHHHHGGSO.......",
    "....OHHHHMMMMGGGGSSO......",
    "....OHGVVVVVVVVVVGSO......",
    "...OHGVVVVVVVVVVVVGSO.....",
    "...OHGVVCCVVVVCCVVVVO.....",
    "...OHGVVCCVVVVCCVVVVO.....",
    "...OHGVVVVVVVVVVVVVVO.....",
    "...OMGVVVVCVCCVVVVGSO.....",
    "...OMGGVVCCVCCVVVGGSO.....",
    "....OMGGVVVVVVVVGGSO......",
    "....OMMGGGGGGGGGGSSO......",
    ".....OMMGGGGGGGGSSO.......",
    "......OOMGGGGGGSOO........",
    "........OOOOOOOO..........",
]


def _build_native_views():
    """Lazily registers custom AppKit NSView subclasses once per process."""
    import objc
    from AppKit import (
        NSBezierPath,
        NSColor,
        NSGradient,
        NSMakeRect,
        NSObject,
        NSRectFill,
        NSView,
    )

    try:
        return (
            objc.lookUpClass("VayuEdgeGlowView"),
            objc.lookUpClass("VayuMascotOrbView"),
            objc.lookUpClass("VayuHUDActionTarget"),
        )
    except objc.error:
        pass

    class VayuHUDActionTarget(NSObject):
        def initWithCallback_(self, cb):
            self = objc.super(VayuHUDActionTarget, self).init()
            if self is not None:
                self._cb = cb
            return self

        def onAction_(self, _sender) -> None:
            if self._cb:
                self._cb()

    class VayuEdgeGlowView(NSView):
        """Draws a Siri-like animated perimeter glow around all four screen edges."""

        def initWithFrame_(self, frame):
            self = objc.super(VayuEdgeGlowView, self).initWithFrame_(frame)
            if self is not None:
                self._phase = 0.0
                self._state_name = AssistantState.HARD_OFF.value
            return self

        def isOpaque(self) -> bool:
            return False

        def setPhase_stateName_(self, phase: float, state_name: str) -> None:
            self._phase = float(phase)
            self._state_name = str(state_name)
            self.setNeedsDisplay_(True)

        def _edge_colors(self):
            p = self._phase
            if self._state_name == AssistantState.HARD_OFF.value:
                c1 = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.95, 0.22, 0.28, 0.85)
                c2 = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.98, 0.45, 0.20, 0.75)
                return c1, c2, c1, c2

            if self._state_name == AssistantState.ACTIVE.value:
                c1 = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.14, 0.90, 0.54, 0.82)
                c2 = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.96, 0.78, 0.28, 0.82)
                return c1, c2, c1, c2

            # Siri / Vayu iridescent wave (Cyan -> Mint Emerald -> Golden Amber -> Electric Violet)
            palette = [
                (0.09, 0.86, 1.00),  # Cyan
                (0.14, 0.90, 0.54),  # Mint Emerald
                (0.96, 0.78, 0.28),  # Vayu Gold
                (0.52, 0.38, 1.00),  # Electric Violet
            ]

            def sample(offset: float):
                idx = (p * 1.4 + offset) % len(palette)
                i0 = int(idx) % len(palette)
                i1 = (i0 + 1) % len(palette)
                frac = idx - int(idx)
                r = palette[i0][0] * (1.0 - frac) + palette[i1][0] * frac
                g = palette[i0][1] * (1.0 - frac) + palette[i1][1] * frac
                b = palette[i0][2] * (1.0 - frac) + palette[i1][2] * frac
                return NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 0.90)

            return sample(0.0), sample(1.0), sample(2.0), sample(3.0)

        def drawRect_(self, _dirty_rect) -> None:
            bounds = self.bounds()
            w = bounds.size.width
            h = bounds.size.height
            if w <= 0 or h <= 0:
                return

            pulse = 0.88 + 0.12 * math.sin(self._phase * 3.2)
            outer_band = 54.0 * pulse
            inner_band = 15.0 * pulse
            clear = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.0, 0.0, 0.0, 0.0)

            c_bottom, c_right, c_top, c_left = self._edge_colors()

            # Soft wide ambient glow on all 4 edges
            strips = [
                (NSMakeRect(0, 0, w, outer_band), c_bottom.colorWithAlphaComponent_(0.34), 90.0),
                (NSMakeRect(0, h - outer_band, w, outer_band), c_top.colorWithAlphaComponent_(0.34), 270.0),
                (NSMakeRect(0, 0, outer_band, h), c_left.colorWithAlphaComponent_(0.34), 0.0),
                (NSMakeRect(w - outer_band, 0, outer_band, h), c_right.colorWithAlphaComponent_(0.34), 180.0),
                # Crisp bright inner neon rim
                (NSMakeRect(0, 0, w, inner_band), c_bottom.colorWithAlphaComponent_(0.78), 90.0),
                (NSMakeRect(0, h - inner_band, w, inner_band), c_top.colorWithAlphaComponent_(0.78), 270.0),
                (NSMakeRect(0, 0, inner_band, h), c_left.colorWithAlphaComponent_(0.78), 0.0),
                (NSMakeRect(w - inner_band, 0, inner_band, h), c_right.colorWithAlphaComponent_(0.78), 180.0),
            ]
            for rect, edge_col, angle in strips:
                grad = NSGradient.alloc().initWithStartingColor_endingColor_(edge_col, clear)
                grad.drawInRect_angle_(rect, angle)

            # Luminous inner bezel hairline
            c_bottom.colorWithAlphaComponent_(0.88).setStroke()
            border = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                NSMakeRect(2.0, 2.0, w - 4.0, h - 4.0), 18.0, 18.0
            )
            border.setLineWidth_(3.0)
            border.stroke()

    class VayuMascotOrbView(NSView):
        """Renders the animated pixel-art golden Vayu robot sphere with floating ears."""

        def initWithFrame_(self, frame):
            self = objc.super(VayuMascotOrbView, self).initWithFrame_(frame)
            if self is not None:
                self._phase = 0.0
                self._state_name = AssistantState.HARD_OFF.value
                self._palette = {
                    "O": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.17, 0.07, 0.05, 1.0),
                    "H": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.99, 0.90, 0.55, 1.0),
                    "G": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.91, 0.69, 0.26, 1.0),
                    "S": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.77, 0.41, 0.15, 1.0),
                    "M": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.14, 0.90, 0.54, 1.0),
                    "V": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.06, 0.08, 0.11, 1.0),
                    "C": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.09, 0.86, 1.00, 1.0),
                    "W": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.72, 0.97, 1.00, 1.0),
                    "R": NSColor.colorWithCalibratedRed_green_blue_alpha_(0.96, 0.28, 0.34, 1.0),
                }
            return self

        def isOpaque(self) -> bool:
            return False

        def setPhase_stateName_(self, phase: float, state_name: str) -> None:
            self._phase = float(phase)
            self._state_name = str(state_name)
            self.setNeedsDisplay_(True)

        def _draw_pixel_rows(
            self,
            rows: list[str],
            start_row: int,
            ox: float,
            oy: float,
            px: float,
        ) -> None:
            for r_idx, row in enumerate(rows):
                grid_y = start_row + r_idx
                # Flip Y for Cocoa bottom-left origin (26-row sprite grid)
                draw_y = oy + (25 - grid_y) * px
                for c_idx, ch in enumerate(row):
                    if ch == ".":
                        continue
                    col = self._palette.get(ch)
                    if col is not None:
                        col.setFill()
                        NSRectFill(NSMakeRect(ox + c_idx * px, draw_y, px + 0.35, px + 0.35))

        def drawRect_(self, _dirty_rect) -> None:
            bounds = self.bounds()
            w = bounds.size.width
            h = bounds.size.height
            cx, cy = w * 0.5, h * 0.5
            p = self._phase

            # State-driven aura color & pulse
            if self._state_name == AssistantState.LISTENING.value:
                aura_rgb = (0.09, 0.86, 1.00)
                pulse = 1.0 + 0.07 * math.sin(p * 6.0)
            elif self._state_name == AssistantState.PROCESSING.value:
                aura_rgb = (0.96, 0.76, 0.25)
                pulse = 1.0 + 0.08 * math.sin(p * 8.0)
            elif self._state_name == AssistantState.ACTIVE.value:
                aura_rgb = (0.14, 0.90, 0.54)
                pulse = 1.02
            else:
                aura_rgb = (0.95, 0.24, 0.30)
                pulse = 0.96

            # Soft glowing sphere aura behind the pixel mascot
            for radius, alpha in [(52.0 * pulse, 0.16), (42.0 * pulse, 0.26), (33.0 * pulse, 0.38)]:
                NSColor.colorWithCalibratedRed_green_blue_alpha_(
                    aura_rgb[0], aura_rgb[1], aura_rgb[2], alpha
                ).setFill()
                oval = NSBezierPath.bezierPathWithOvalInRect_(
                    NSMakeRect(cx - radius, cy - radius, radius * 2.0, radius * 2.0)
                )
                oval.fill()

            # Crisp pixel scale (26x26 sprite centered inside 116x116 view)
            px = 3.4
            sprite_size = 26.0 * px
            hover_y = math.sin(p * 3.0) * 2.6 if self._state_name != AssistantState.HARD_OFF.value else -1.5
            ear_bob = math.sin(p * 4.2 + 0.7) * 2.4 if self._state_name != AssistantState.HARD_OFF.value else -2.0

            ox = round(cx - sprite_size * 0.5)
            oy = round(cy - sprite_size * 0.5 + hover_y)

            # 1. Draw main golden sphere body
            self._draw_pixel_rows(_SPHERE_GRID, start_row=8, ox=ox, oy=oy, px=px)

            # 2. Draw floating triangular cat ears with independent vertical bob
            self._draw_pixel_rows(_EAR_GRID, start_row=0, ox=ox, oy=round(oy + ear_bob), px=px)

            # 3. Dynamic state expressions overlaid cleanly on the dark visor
            if self._state_name == AssistantState.LISTENING.value:
                # Bright cyan eye glints + animated voice-reactive mouth pulse
                self._palette["W"].setFill()
                NSRectFill(NSMakeRect(ox + 8 * px, oy + (25 - 14) * px, px, px))
                NSRectFill(NSMakeRect(ox + 14 * px, oy + (25 - 14) * px, px, px))
                if math.sin(p * 10.0) > 0.15:
                    self._palette["C"].setFill()
                    NSRectFill(NSMakeRect(ox + 11 * px, oy + (25 - 17) * px, 2 * px, px))

            elif self._state_name == AssistantState.PROCESSING.value:
                # Scanning cyan/gold indicator across the visor
                scan_x = 8 + int((math.sin(p * 7.5) + 1.0) * 3.5)
                self._palette["W"].setFill()
                NSRectFill(NSMakeRect(ox + scan_x * px, oy + (25 - 14) * px, 2 * px, px))

            elif self._state_name == AssistantState.HARD_OFF.value:
                # Dim visor & sleepy horizontal eyes
                self._palette["V"].setFill()
                NSRectFill(NSMakeRect(ox + 7 * px, oy + (25 - 18) * px, 10 * px, 5 * px))
                self._palette["R"].setFill()
                NSRectFill(NSMakeRect(ox + 8 * px, oy + (25 - 15) * px, 2 * px, px * 0.7))
                NSRectFill(NSMakeRect(ox + 14 * px, oy + (25 - 15) * px, 2 * px, px * 0.7))

    return VayuEdgeGlowView, VayuMascotOrbView, VayuHUDActionTarget


class FloatingHUDWindow:
    """
    Native macOS Siri-inspired HUD:
      1. Full-screen iridescent glowing screen border (click-through).
      2. Floating animated golden pixel-art Vayu sphere + status pill + KILL MIC button.
    """

    def __init__(self, on_kill_mic: Callable[[], None]) -> None:
        self._on_kill_mic = on_kill_mic
        self._panel = None
        self._edge_panel = None
        self._edge_view = None
        self._orb_view = None
        self._title_label = None
        self._detail_label = None
        self._action_target = None
        self._anim_target = None
        self._anim_timer = None
        self._phase: float = 0.0
        self._last_tick: float = time.monotonic()
        self._current_state: AssistantState = AssistantState.HARD_OFF
        self._is_visible: bool = False

    def _ensure_window(self) -> bool:
        if self._panel is not None:
            return True
        try:
            from AppKit import (
                NSBackingStoreBuffered,
                NSBezelStyleRounded,
                NSButton,
                NSColor,
                NSFloatingWindowLevel,
                NSFont,
                NSMakeRect,
                NSPanel,
                NSRunLoop,
                NSRunLoopCommonModes,
                NSScreen,
                NSTextField,
                NSTimer,
                NSView,
                NSWindowCollectionBehaviorCanJoinAllSpaces,
                NSWindowCollectionBehaviorFullScreenAuxiliary,
                NSWindowStyleMaskBorderless,
                NSWindowStyleMaskNonactivatingPanel,
            )

            EdgeGlowViewCls, MascotOrbViewCls, ActionTargetCls = _build_native_views()

            screen = NSScreen.mainScreen()
            full_frame = screen.frame() if screen else NSMakeRect(0, 0, 1440, 900)
            vis_frame = screen.visibleFrame() if screen else NSMakeRect(0, 0, 1440, 900)

            style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
            behavior = (
                NSWindowCollectionBehaviorCanJoinAllSpaces
                | NSWindowCollectionBehaviorFullScreenAuxiliary
            )

            # 1. Full-screen Siri-style glowing screen edge overlay (click-through)
            edge_panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                full_frame,
                style,
                NSBackingStoreBuffered,
                False,
            )
            edge_panel.setLevel_(NSFloatingWindowLevel)
            edge_panel.setCollectionBehavior_(behavior)
            edge_panel.setOpaque_(False)
            edge_panel.setBackgroundColor_(NSColor.clearColor())
            edge_panel.setHasShadow_(False)
            edge_panel.setIgnoresMouseEvents_(True)

            edge_view = EdgeGlowViewCls.alloc().initWithFrame_(
                NSMakeRect(0, 0, full_frame.size.width, full_frame.size.height)
            )
            edge_panel.setContentView_(edge_view)

            # 2. Floating Vayu Orb + Glass Pill HUD at bottom-center
            hud_w, hud_h = 410.0, 184.0
            x_pos = vis_frame.origin.x + (vis_frame.size.width - hud_w) / 2.0
            y_pos = vis_frame.origin.y + 24.0

            panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(x_pos, y_pos, hud_w, hud_h),
                style,
                NSBackingStoreBuffered,
                False,
            )
            panel.setLevel_(NSFloatingWindowLevel + 1)
            panel.setCollectionBehavior_(behavior)
            panel.setOpaque_(False)
            panel.setBackgroundColor_(NSColor.clearColor())
            panel.setHasShadow_(False)

            root_view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, hud_w, hud_h))
            panel.setContentView_(root_view)

            # Centered floating pixel-art Vayu sphere (top half of HUD)
            orb_size = 116.0
            orb_view = MascotOrbViewCls.alloc().initWithFrame_(
                NSMakeRect((hud_w - orb_size) / 2.0, 64.0, orb_size, orb_size)
            )
            root_view.addSubview_(orb_view)

            # Sleek translucent status pill below the Vayu sphere
            pill_w, pill_h = 396.0, 62.0
            pill_view = NSView.alloc().initWithFrame_(
                NSMakeRect((hud_w - pill_w) / 2.0, 2.0, pill_w, pill_h)
            )
            pill_view.setWantsLayer_(True)
            pill_layer = pill_view.layer()
            pill_layer.setCornerRadius_(18.0)
            pill_layer.setBackgroundColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(0.07, 0.09, 0.13, 0.90).CGColor()
            )
            pill_layer.setBorderWidth_(1.0)
            pill_layer.setBorderColor_(
                NSColor.colorWithCalibratedRed_green_blue_alpha_(0.22, 0.78, 0.96, 0.42).CGColor()
            )
            root_view.addSubview_(pill_view)

            title_label = NSTextField.labelWithString_("Vayu")
            title_label.setFrame_(NSMakeRect(16, 33, 272, 20))
            title_label.setFont_(NSFont.boldSystemFontOfSize_(13.0))
            title_label.setTextColor_(NSColor.whiteColor())
            pill_view.addSubview_(title_label)

            detail_label = NSTextField.labelWithString_("Ready")
            detail_label.setFrame_(NSMakeRect(16, 10, 272, 20))
            detail_label.setFont_(NSFont.systemFontOfSize_(11.5))
            detail_label.setTextColor_(NSColor.colorWithCalibratedWhite_alpha_(0.82, 1.0))
            pill_view.addSubview_(detail_label)

            self._action_target = ActionTargetCls.alloc().initWithCallback_(self._on_kill_mic)
            kill_btn = NSButton.buttonWithTitle_target_action_(
                "KILL MIC", self._action_target, "onAction:"
            )
            kill_btn.setFrame_(NSMakeRect(294, 14, 88, 34))
            kill_btn.setBezelStyle_(NSBezelStyleRounded)
            kill_btn.setContentTintColor_(NSColor.systemRedColor())
            pill_view.addSubview_(kill_btn)

            # 30 FPS animation timer for smooth orb hover + Siri screen edge wave
            self._anim_target = ActionTargetCls.alloc().initWithCallback_(self._tick_animation)
            timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0 / 30.0, self._anim_target, "onAction:", None, True
            )
            NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)

            self._edge_panel = edge_panel
            self._edge_view = edge_view
            self._panel = panel
            self._orb_view = orb_view
            self._title_label = title_label
            self._detail_label = detail_label
            self._anim_timer = timer
            return True
        except Exception:
            return False

    def _tick_animation(self) -> None:
        if not self._is_visible or self._orb_view is None:
            return
        now = time.monotonic()
        dt = min(now - self._last_tick, 0.1)
        self._last_tick = now
        self._phase += dt

        state_val = self._current_state.value
        self._orb_view.setPhase_stateName_(self._phase, state_val)
        if self._edge_view is not None:
            self._edge_view.setPhase_stateName_(self._phase, state_val)

    def render(self, view: HUDViewModel) -> None:
        def _update() -> None:
            if not self._ensure_window():
                return
            self._current_state = view.state
            self._is_visible = view.visible

            if view.visible:
                self._last_tick = time.monotonic()
                self._title_label.setStringValue_(view.headline)
                self._detail_label.setStringValue_(view.detail)
                self._orb_view.setPhase_stateName_(self._phase, view.state.value)
                self._edge_view.setPhase_stateName_(self._phase, view.state.value)

                # Show Siri screen-edge glow during active wake/listening/processing states
                show_edge = view.state in (
                    AssistantState.LISTENING,
                    AssistantState.PROCESSING,
                    AssistantState.ACTIVE,
                )
                if show_edge:
                    self._edge_panel.orderFrontRegardless()
                else:
                    self._edge_panel.orderOut_(None)

                self._panel.orderFrontRegardless()
            else:
                self._edge_panel.orderOut_(None)
                self._panel.orderOut_(None)

        _run_on_main_thread(_update)


class HUDController:
    """
    Manages the floating Vayu mascot orb, Siri-style edge glow, auto-hide timer,
    and explicit user confirmation prompts for quit actions and unbookmarked domains.
    """

    def __init__(
        self,
        on_kill_mic: Callable[[], None],
        confirm_handler: Callable[[str, str], bool] | None = None,
        enable_native_window: bool = True,
    ) -> None:
        self.on_kill_mic = on_kill_mic
        self.confirm_handler = confirm_handler or self._default_macos_confirm
        self.view = HUDViewModel()
        self.state: AssistantState = AssistantState.HARD_OFF
        self.is_visible: bool = False
        self._lock = threading.RLock()
        self._hide_timer: threading.Timer | None = None
        self._window = FloatingHUDWindow(on_kill_mic=self.trigger_kill_switch) if enable_native_window else None

    def _cancel_hide_timer(self) -> None:
        if self._hide_timer is not None:
            self._hide_timer.cancel()
            self._hide_timer = None

    def show_status(
        self,
        state: AssistantState,
        headline: str,
        detail: str = "",
        auto_hide_seconds: float | None = None,
    ) -> None:
        with self._lock:
            self._cancel_hide_timer()
            self.state = state
            self.is_visible = True
            self.view = HUDViewModel(
                state=state,
                headline=headline,
                detail=detail,
                visible=True,
            )
            if self._window is not None:
                self._window.render(self.view)

            if auto_hide_seconds is not None and auto_hide_seconds > 0:
                self._hide_timer = threading.Timer(auto_hide_seconds, self.hide)
                self._hide_timer.daemon = True
                self._hide_timer.start()

    def hide(self) -> None:
        with self._lock:
            self._cancel_hide_timer()
            self.is_visible = False
            self.view.visible = False
            if self._window is not None:
                self._window.render(self.view)

    def trigger_kill_switch(self) -> None:
        """Invoked when the user clicks KILL MIC directly on the floating HUD."""
        self.on_kill_mic()
        self.show_status(
            state=AssistantState.HARD_OFF,
            headline="🔴 Vayu: Hard Off",
            detail="Microphone stream closed & RAM buffer wiped",
            auto_hide_seconds=2.0,
        )

    def click_kill_mic(self) -> None:
        self.trigger_kill_switch()

    def request_confirmation(self, title: str, prompt: str) -> bool:
        """Blocks until the user explicitly approves or denies a gated action."""
        self.show_status(
            state=AssistantState.PROCESSING,
            headline=f"⚠️ {title}",
            detail=prompt,
            auto_hide_seconds=None,
        )
        approved = bool(self.confirm_handler(title, prompt))
        if not approved:
            self.show_status(
                state=AssistantState.ACTIVE,
                headline="Action Cancelled",
                detail="Blocked unconfirmed command",
                auto_hide_seconds=2.0,
            )
        return approved

    @staticmethod
    def _default_macos_confirm(title: str, prompt: str) -> bool:
        """Displays a native macOS modal confirmation dialog on the main thread."""
        result_holder = {"approved": False}
        done_event = threading.Event()

        def _ask() -> None:
            try:
                import rumps

                response = rumps.alert(
                    title=f"Vayu Security Check — {title}",
                    message=prompt,
                    ok="Allow",
                    cancel="Deny",
                )
                result_holder["approved"] = response == 1
            finally:
                done_event.set()

        _run_on_main_thread(_ask)
        done_event.wait(timeout=30.0)
        return result_holder["approved"]