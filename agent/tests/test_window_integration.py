"""Настоящий Tk: содержимое остаётся доступно на невысоком экране и при увеличенном DPI."""

from types import SimpleNamespace

import pytest

from studio_agent import store, window


@pytest.fixture(scope="module")
def tk_app():
    """Один интерпретатор Tk, как у приложения; каждый тест получает чистое окно."""
    import tkinter as tk

    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def tk_root(tk_app):
    scale = tk_app.tk.call("tk", "scaling")
    yield tk_app
    for child in tk_app.winfo_children():
        child.destroy()
    tk_app.update()
    tk_app.tk.call("tk", "scaling", scale)


@pytest.mark.integration
@pytest.mark.parametrize("scale", [1.0, 1.5, 2.0])
@pytest.mark.parametrize("state", ["setup", "online", "offline", "revoked", "replaced"])
def test_short_screen_keeps_all_controls_reachable(monkeypatch, scale, state, tk_root):
    root = tk_root
    assert all(value > 0 for value in window._work_area(root)[2:])
    root.tk.call("tk", "scaling", scale * 96 / 72)
    cfg = store.Config(projects_root="C:/work/projects", max_turns=8)
    ctrl = SimpleNamespace(state=state, studio_version="0.1.0", config=lambda: cfg, autostart_enabled=lambda: False,
                           backend_status=lambda: {}, login="owner", pin_status=lambda: "set",
                           activity_status=lambda: {"tasks": 0, "waiting": 0, "desktop_active": False,
                                                    "quit_when_idle": False},
                           stop_desktop_session=lambda: None, set_quit_when_idle=lambda enabled: None,
                           request_quit=lambda: None)
    monkeypatch.setattr(window.MainWindow, "lift", lambda self: None)
    monkeypatch.setattr(window.MainWindow, "work", lambda *args, **kwargs: None)
    # Подмена только границ монитора: настоящую геометрию и прокрутку рассчитывает Tk.
    monkeypatch.setattr(window, "_work_area", lambda win: (0, 0, 1274, 733), raising=False)
    main = window.MainWindow(root, ctrl)
    main.pin = "set"
    if state != "setup":
        main.show_pin()
    root.update()
    # Обычное окно укладывается в 440 логических пикселей без уменьшения шрифта.
    assert main.body.winfo_reqheight() <= main.px(440)
    assert main.win.winfo_height() <= 733 - main.px(48)
    assert main.viewport.winfo_height() <= 733 - main.px(48)
    main.viewport.yview_moveto(1)
    root.update()
    assert main.body.winfo_rooty() + main.body.winfo_height() <= \
        main.viewport.winfo_rooty() + main.viewport.winfo_height() + 1
    if state != "setup":
        main.sections.select(main.settings_page)
        root.update()
        assert main.pin_line.winfo_rooty() >= main.viewport.winfo_rooty()
        main.viewport.yview_moveto(0)
        main.pin_line.winfo_children()[-1].focus_force()
        main.pin_line.winfo_children()[-1].event_generate("<FocusIn>")
        root.update()
        pin_bottom = main.pin_line.winfo_rooty() + main.pin_line.winfo_height()
        assert pin_bottom <= main.viewport.winfo_rooty() + main.viewport.winfo_height() + 1
    root.update()
    place = main.viewport.yview()
    main.apply_theme("dark")
    main.apply_theme("light")
    root.update()
    # Смена темы перекрашивает на месте: прокрутка не сдвигается. Прежнее `== 1.0` держалось только при одном порядке
    # случаев — строка PIN выше в фокусе прокручивает окно ровно до неё, а не до конца.
    assert main.viewport.yview() == place


@pytest.mark.integration
def test_tabs_keyboard_and_theme_keep_settings_and_live_status(home, monkeypatch, tk_root):
    from tkinter import ttk
    from studio_agent.app import Controller

    store.save_config(home, store.Config(projects_root="C:/work/projects", theme="light"))
    ctrl = Controller(home, autostart=SimpleNamespace(enabled=lambda: False))
    ctrl.state = "online"
    ctrl.studio_version = "0.1.0"
    memory = {"available": 4 * 1024 ** 3, "total": 16 * 1024 ** 3}
    monkeypatch.setattr(window.computer_info, "memory_status", lambda: memory)
    root = tk_root
    monkeypatch.setattr(window.MainWindow, "lift", lambda self: None)
    monkeypatch.setattr(window.MainWindow, "work", lambda self, fn, done, **kwargs: done(fn(), False))
    monkeypatch.setattr(window, "_work_area", lambda win: (0, 0, 1024, 600))
    main = window.MainWindow(root, ctrl)
    main.pin = "locked"
    main.show_pin()
    root.update()
    assert main.studio_version_value.cget("text") == "0.1.0"
    assert main.memory_value.cget("text") == "Доступно 4,0 из 16,0 ГБ"
    memory["available"] = 0
    main.refresh_connection_info()
    assert main.memory_value.cget("text") == "Доступно 0,0 из 16,0 ГБ"
    assert [main.sections.tab(tab, "text") for tab in main.sections.tabs()] == [
        "Подключение", "Работа", "Настройки"]
    main.sections.focus_force()
    for page in (main.activity_page, main.settings_page):
        main.sections.event_generate("<Control-Tab>")
        root.update()
        assert main.sections.select() == str(page)
    limit = next(widget for widget in main.buttons if isinstance(widget, ttk.Entry))
    limit.delete(0, "end")
    limit.insert(0, "11")
    for label, choice in (("Тёмная", "dark"), ("Светлая", "light")):
        radio = next(widget for widget in main.buttons if isinstance(widget, ttk.Radiobutton)
                     and widget.cget("text") == label)
        radio.invoke()
        root.update()
        assert store.load_config(home).theme == choice
        assert main.sections.select() == str(main.settings_page)
        assert limit.get() == "11"
    main.on_state("offline")
    assert main.studio_version_value.cget("text") == "Нет связи"
    main.show_login("new-owner")
    assert main.sections.select() == str(main.settings_page)
    assert main.card_widgets[2].cget("text") == window.STATUS["offline"][0]
    assert main.login_value.cget("text") == "new-owner"
    limit.delete(0, "end")
    limit.insert(0, "0")
    save = next(widget for widget in limit.master.winfo_children() if isinstance(widget, ttk.Button))
    save.invoke()
    root.update()
    assert main.settings_problem.get() == "Число заданий — целое число больше нуля"
    limit.delete(0, "end")
    limit.insert(0, "11")
    save.invoke()
    root.update()
    assert store.load_config(home).max_turns == 11 and not main.settings_problem.get()
    assert main.win.winfo_height() <= 600 - main.px(48)
    main.sections.event_generate("<Control-Shift-Tab>")
    root.update()
    assert main.sections.select() == str(main.activity_page)


@pytest.mark.integration
def test_local_desktop_checkbox_persists_without_restarting_or_contacting_studio(home, monkeypatch, tk_root):
    from tkinter import ttk
    from studio_agent.app import Controller

    store.save_config(home, store.Config(projects_root='C:/work/projects'))
    ctrl = Controller(home, autostart=SimpleNamespace(enabled=lambda: False))
    ctrl.state = 'online'
    root = tk_root
    monkeypatch.setattr(window.MainWindow, 'lift', lambda self: None)
    monkeypatch.setattr(window.MainWindow, 'work', lambda self, fn, done, **kwargs: done(fn(), False))

    def checkbox(main):
        return next(widget for widget in main.buttons if isinstance(widget, ttk.Checkbutton)
                    and widget.cget('text') == 'Разрешить удалённый рабочий стол')

    main = window.MainWindow(root, ctrl)
    root.update()
    assert not checkbox(main).instate(['selected'])
    checkbox(main).invoke()
    root.update()
    assert store.load_config(home).desktop_enabled is True
    main.close()
    another = Controller(home, autostart=SimpleNamespace(enabled=lambda: False))
    another.state = 'online'
    restarted = window.MainWindow(root, another)
    root.update()
    assert checkbox(restarted).instate(['selected'])
    restarted.ctrl.set_desktop_enabled(False)
    assert not store.load_config(home).desktop_enabled
    assert ctrl.agent is None and ctrl._link is None


@pytest.mark.integration
def test_local_activity_buttons_stop_only_desktop_and_wait_for_work_before_exit(home, monkeypatch, tk_root):
    from studio_agent.app import Controller

    store.save_config(home, store.Config(projects_root='C:/work/projects', desktop_enabled=True))
    ctrl = Controller(home, autostart=SimpleNamespace(enabled=lambda: False))
    ctrl.state = 'online'
    desktop = {"active": True, "enabled": True}
    ctrl.agent = SimpleNamespace(desktop=SimpleNamespace(status=lambda: dict(desktop),
        revoke=lambda: desktop.update(active=False)))
    work = {"task": True}
    ctrl.codex = SimpleNamespace(live=lambda: dict(work))
    exits = []
    ctrl.on_idle_quit = lambda: exits.append(True)
    ctrl._refresh_activity()
    root = tk_root
    monkeypatch.setattr(window.MainWindow, 'lift', lambda self: None)
    monkeypatch.setattr(window.MainWindow, 'work', lambda self, fn, done, **kwargs: done(fn(), False))
    main = window.MainWindow(root, ctrl)
    root.update()
    tasks, label, stop, idle, immediate = main.activity_widgets
    assert tasks.cget('text') == 'Заданий: 1 · ждут ответа: 1'
    assert label.cget('text') == 'Удалённый рабочий стол подключён'
    stop.invoke()
    ctrl._refresh_activity()
    main.show_activity()
    root.update()
    assert not desktop['active'] and ctrl.config().desktop_enabled
    assert stop.instate(['disabled']) and label.cget('text').endswith('не подключён')
    idle.invoke()
    main.show_activity()
    assert not exits and idle.cget('text') == 'Отменить выход'
    idle.invoke()
    work.clear()
    ctrl._refresh_activity()
    main.show_activity()
    assert not exits
    idle.invoke()
    assert exits == [True]
    immediate.invoke()
    assert exits == [True, True]
