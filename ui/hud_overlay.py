from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class AssistantState(str, Enum):
    HARD_OFF = "hard_off"
    ACTIVE = "active"
    LISTENING = "listening"
    PROCESSING = "processing"


@dataclass
class HUDViewState:
    state: AssistantState = AssistantState.HARD_OFF
    headline: str = "🔴 Vayu: Hard Off"
    detail: str = ""
    is_visible: bool = False

    @property
    def visible(self) -> bool:
        return self.is_visible


def _dispatch_main(fn: Callable[[], None]) -> None:
    """Runs AppKit UI operations safely on the macOS main runloop."""
    try:
        from AppKit import NSThread
        from PyObjCTools import AppHelper

        if NSThread.isMainThread():
            fn()
        else:
            AppHelper.callAfter(fn)
    except Exception:
        fn()


class HUDController:
    """
    Native macOS floating HUD pill (NSPanel) + explicit confirmation dialog.
    Marshals all Cocoa window operations to the main thread so state transitions
    (Listening Blue -> Complete/Active Green -> Auto-Hide) never freeze.
    """

    def __init__(
        self,
        on_kill_mic: Callable[[], None] | None = None,
        confirm_handler: Callable[[str, str], bool] | None = None,
        enable_native_window: bool = True,
    ) -> None:
        self.on_kill_mic = on_kill_mic
        self.confirm_handler = confirm_handler
        self.enable_native_window = enable_native_window

        self.view = HUDViewState()
        self._hide_timer: threading.Timer | None = None
        self._panel = None
        self._title_field = None
        self._detail_field = None

    @property
    def state(self) -> AssistantState:
        return self.view.state

    @state.setter
    def state(self, value: AssistantState) -> None:
        self.view.state = value

    @property
    def headline(self) -> str:
        return self.view.headline

    @headline.setter
    def headline(self, value: str) -> None:
        self.view.headline = value

    @property
    def detail(self) -> str:
        return self.view.detail

    @detail.setter
    def detail(self, value: str) -> None:
        self.view.detail = value

    @property
    def is_visible(self) -> bool:
        return self.view.is_visible

    @is_visible.setter
    def is_visible(self, value: bool) -> None:
        self.view.is_visible = value

    def _ensure_native_panel(self) -> None:
        if self._panel is not None or not self.enable_native_window:
            return

        import objc
        from AppKit import (
            NSBackingStoreBuffered,
            NSButton,
            NSColor,
            NSFloatingWindowLevel,
            NSFont,
            NSMakeRect,
            NSPanel,
            NSScreen,
            NSTextField,
            NSWindowStyleMaskBorderless,
            NSWindowStyleMaskNonactivatingPanel,
        )

        screen = NSScreen.mainScreen()
        frame = screen.frame() if screen else NSMakeRect(0, 0, 1440, 900)
        width, height = 430.0, 68.0
        x = (frame.size.width - width) / 2.0
        y = 72.0

        style = NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, width, height),
            style,
            NSBackingStoreBuffered,
            False,
        )
        panel.setLevel_(NSFloatingWindowLevel)
        panel.setOpaque_(False)
        panel.setHasShadow_(True)
        panel.setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.08, 0.10, 0.14, 0.94)
        )

        content = panel.contentView()
        content.setWantsLayer_(True)
        content.layer().setCornerRadius_(16.0)

        title_field = NSTextField.labelWithString_(self.headline)
        title_field.setFrame_(NSMakeRect(16, 36, 305, 22))
        title_field.setFont_(NSFont.boldSystemFontOfSize_(13.5))
        title_field.setTextColor_(NSColor.whiteColor())
        content.addSubview_(title_field)

        detail_field = NSTextField.labelWithString_(self.detail)
        detail_field.setFrame_(NSMakeRect(16, 12, 305, 20))
        detail_field.setFont_(NSFont.systemFontOfSize_(11.5))
        detail_field.setTextColor_(NSColor.lightGrayColor())
        content.addSubview_(detail_field)

        hud_ref = self

        class _KillAction(objc.lookUpClass("NSObject")):
            def onKill_(self, _sender) -> None:
                hud_ref.trigger_kill_switch()

        self._kill_target = _KillAction.alloc().init()
        kill_btn = NSButton.alloc().initWithFrame_(NSMakeRect(330, 18, 86, 32))
        kill_btn.setTitle_("KILL MIC")
        kill_btn.setBezelStyle_(1)
        kill_btn.setTarget_(self._kill_target)
        kill_btn.setAction_("onKill:")
        content.addSubview_(kill_btn)

        self._panel = panel
        self._title_field = title_field
        self._detail_field = detail_field

    def _cancel_hide_timer(self) -> None:
        if self._hide_timer is not None:
            self._hide_timer.cancel()
            self._hide_timer = None

    def show_status(
        self,
        state: AssistantState,
        headline: str,
        detail: str = "",
        auto_hide_seconds: float | None = 2.5,
    ) -> None:
        self._cancel_hide_timer()
        self.state = state
        self.headline = headline
        self.detail = detail
        self.is_visible = True

        if self.enable_native_window:
            def _update_ui() -> None:
                self._ensure_native_panel()
                if self._panel is None:
                    return

                from AppKit import NSColor

                tint_map = {
                    AssistantState.LISTENING: (0.07, 0.22, 0.45, 0.95),
                    AssistantState.PROCESSING: (0.10, 0.25, 0.48, 0.95),
                    AssistantState.ACTIVE: (0.07, 0.28, 0.18, 0.95),
                    AssistantState.HARD_OFF: (0.38, 0.09, 0.12, 0.95),
                }
                r, g, b, a = tint_map.get(state, (0.08, 0.10, 0.14, 0.94))
                self._panel.setBackgroundColor_(
                    NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)
                )
                self._title_field.setStringValue_(headline)
                self._detail_field.setStringValue_(detail)
                self._panel.orderFrontRegardless()

            _dispatch_main(_update_ui)

        if auto_hide_seconds is not None and auto_hide_seconds > 0:
            self._hide_timer = threading.Timer(auto_hide_seconds, self.hide)
            self._hide_timer.daemon = True
            self._hide_timer.start()

    def hide(self) -> None:
        self._cancel_hide_timer()
        self.is_visible = False
        if self.enable_native_window:
            def _order_out() -> None:
                if self._panel is not None:
                    self._panel.orderOut_(None)

            _dispatch_main(_order_out)

    def trigger_kill_switch(self) -> None:
        """Invoked when the user clicks KILL MIC on the floating HUD."""
        if self.on_kill_mic:
            self.on_kill_mic()
        self.show_status(
            state=AssistantState.HARD_OFF,
            headline="🔴 Vayu: Hard Off",
            detail="Stream torn down + OS permission gated",
            auto_hide_seconds=2.0,
        )

    def click_kill_mic(self) -> None:
        """Alias for trigger_kill_switch."""
        self.trigger_kill_switch()

    def request_confirmation(self, title: str, prompt: str) -> bool:
        """Shows a blocking native macOS confirmation dialog safely on the main thread."""
        if self.confirm_handler is not None:
            return bool(self.confirm_handler(title, prompt))
        if not self.enable_native_window:
            return False

        from AppKit import NSAlert, NSAlertFirstButtonReturn, NSThread
        from PyObjCTools import AppHelper

        def _show_alert() -> bool:
            alert = NSAlert.alloc().init()
            alert.setMessageText_(title)
            alert.setInformativeText_(prompt)
            alert.addButtonWithTitle_("Allow")
            alert.addButtonWithTitle_("Cancel")
            response = alert.runModal()
            return response == NSAlertFirstButtonReturn

        if NSThread.isMainThread():
            return _show_alert()

        done = threading.Event()
        outcome = [False]

        def _worker() -> None:
            try:
                outcome[0] = _show_alert()
            finally:
                done.set()

        AppHelper.callAfter(_worker)
        done.wait()
        return outcome[0]