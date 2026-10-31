"""Настоящий Tk: содержимое остаётся доступно на невысоком экране и при увеличенном DPI."""

from types import SimpleNamespace

import pytest

from studio_agent import store, window


@pytest.mark.integration
@pytest.mark.parametrize("scale", [1.0, 1.5])
@pytest.mark.parametrize("state", ["setup", "online"])
def test_short_screen_keeps_all_controls_reachable(monkeypatch, scale, state):
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    assert all(value > 0 for value in window._work_area(root)[2:])
    root.tk.call("tk", "scaling", scale * 96 / 72)
    cfg = store.Config(projects_root="C:/work/projects", max_turns=8)
    ctrl = SimpleNamespace(state=state, config=lambda: cfg, autostart_enabled=lambda: False,
                           backend_status=lambda: {}, login=lambda: "owner", pin_status=lambda: "set")
    monkeypatch.setattr(window.MainWindow, "lift", lambda self: None)
    monkeypatch.setattr(window.MainWindow, "work", lambda *args, **kwargs: None)
    # Подмена только границ монитора: настоящую геометрию и прокрутку рассчитывает Tk.
    monkeypatch.setattr(window, "_work_area", lambda win: (0, 0, 1274, 733), raising=False)
    try:
        main = window.MainWindow(root, ctrl)
        main.pin = "set"
        if state != "setup":
            main.show_pin()
        root.update()
        assert main.win.winfo_height() <= 733 - main.px(48)
        assert main.viewport.winfo_height() <= 733 - main.px(48)
        main.viewport.yview_moveto(1)
        root.update()
        assert main.body.winfo_rooty() + main.body.winfo_height() <= \
            main.viewport.winfo_rooty() + main.viewport.winfo_height() + 1
        if state != "setup":
            assert main.pin_line.winfo_rooty() >= main.viewport.winfo_rooty()
            main.viewport.yview_moveto(0)
            main.pin_line.winfo_children()[-1].focus_force()
            main.pin_line.winfo_children()[-1].event_generate("<FocusIn>")
            root.update()
            pin_bottom = main.pin_line.winfo_rooty() + main.pin_line.winfo_height()
            assert pin_bottom <= main.viewport.winfo_rooty() + main.viewport.winfo_height() + 1
        main.apply_theme("dark")
        main.apply_theme("light")
        root.update()
        assert main.viewport.yview()[1] == 1.0
    finally:
        root.destroy()
