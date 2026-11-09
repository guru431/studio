"""Сведения о проектах, сортировка и закреплённое обновление в браузере."""

import pytest

from fake_bridge import FakeMachine, T0, pid
from test_ui_browser import READY, browser, code_online, code_requests, go, live, problems, set_value, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize("width,timezone", [(320, "UTC"), (1280, "America/New_York")])
def test_project_times_use_moscow_across_browser_timezones(browser, live, width, timezone):
    login = f"project-timezone-{width}"
    live.add_user(login)
    stamp = 1_767_216_645  # 2025-12-31 21:30:45 UTC — уже 1 января в Москве.
    machine = FakeMachine({"alpha": {"summary": {"modified": stamp, "checked_at": stamp,
        "git": "ok", "github": {"state": "current", "behind": 0, "ahead": 0}}},
        "unknown": {"summary": {"git": "ok"}}})
    machine.project_summary = True
    _, server = code_online(live, login, machine)
    alpha = f"#code-projects li[data-pid='{pid('alpha')}']"
    unknown = f"#code-projects li[data-pid='{pid('unknown')}']"
    ready = "document.querySelector('#code-projects li') && document.querySelector('#code-project-updated')?.textContent.startsWith('Обновлено:')"
    dates = f"""(() => {{
      const row = document.querySelector("{alpha}");
      const modified = row.querySelector('.code-project-modified');
      return [modified.textContent, modified.title,
        row.querySelector('.code-project-summary').title,
        row.querySelector('.code-project-github').title,
        document.querySelector('#code-project-updated').textContent,
        document.querySelector("{unknown} .code-project-modified").textContent];
    }})()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
            ("eval", "timezone", "Intl.DateTimeFormat().resolvedOptions().timeZone"),
            ("eval", "clock", f"Date.now = () => {stamp * 1000}; true"),
            *go("code"), ("wait", ready), ("eval", "initial", dates),
            ("click", "#code-project-refresh"), ("wait", ready), ("eval", "refreshed", dates)],
            width=width, mobile=width == 320, timezone=timezone)
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["timezone"] == timezone
    expected = ["01.01.26, 00:30", "Изменён: 01.01.2026, 00:30:45",
        "Проверено: 01.01.2026, 00:30:45",
        "GitHub синхронизирован с текущей веткой. Проверено: 01.01.2026, 00:30:45",
        "Обновлено: 01.01.2026, 00:30:45", "—"]
    assert result["values"]["initial"] == expected
    assert result["values"]["refreshed"] == expected
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width,visibility", [(320, "public"), (1280, "private")])
def test_project_sort_restores_after_navigation_reload_and_account_switch(browser, live, width, visibility):
    login, other = f"project-sort-{width}", f"project-sort-other-{width}"
    live.add_user(login)
    live.add_user(other)
    projects = {name: {"summary": {"findings": count, "ideas": count, "changed": 0, "unpushed": 0,
        "modified": T0, "checked_at": T0, "git": "ok"}} for name, count in (("alpha", 1), ("beta", 5))}
    projects["alpha"]["summary"]["github"] = None
    projects["beta"]["summary"]["github"] = {"state": "current", "behind": 0, "ahead": 0}
    machine, second = FakeMachine(projects), FakeMachine(projects)
    machine.project_summary = second.project_summary = True
    aid, server = code_online(live, login, machine)
    bid, other_server = code_online(live, other, second)
    ready = "document.querySelector('#code-projects li') && document.querySelector('#code-project-updated')?.textContent.startsWith('Обновлено:')"
    selection = """[document.querySelector('#code-project-sort').value,
      document.querySelector('.code-project-sort-text').textContent,
      document.querySelector('#code-projects li').dataset.pid]"""
    logout = [*go("settings/code"), ("wait", "!!document.querySelector('#code-defaults-save')"),
              ("click", "#avatar"), ("click", "#menu-logout"),
              ("wait", "location.pathname === '/login' && document.querySelector('#login')")]
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), ("wait", ready),
            set_value("#code-project-sort", visibility), ("eval", "chosen", selection),
            *go("settings/code"), ("wait", "!!document.querySelector('#code-defaults-save')"),
            *go("code"), ("wait", ready), ("eval", "returned", selection),
            ("eval", "reload", "location.reload(); true"), ("wait", READY), ("wait", ready),
            ("eval", "reloaded", selection),
            *logout, *signin(other), ("wait", READY), *go("code"), ("wait", ready),
            ("eval", "other", selection),
            *logout, *signin(login), ("wait", READY), *go("code"), ("wait", ready),
            ("eval", "signedInAgain", selection)], width=width, mobile=width == 320)
    finally:
        server.stop()
        other_server.stop()
    assert not result.get("failed"), result
    label = "Сначала публичные" if visibility == "public" else "Сначала приватные"
    for key in ("chosen", "returned", "reloaded", "signedInAgain"):
        assert result["values"][key] == [visibility, label, pid("beta" if visibility == "public" else "alpha")], result["values"]
    assert result["values"]["other"] == ["name", "По имени", pid("alpha")]
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_project_visibility_sort_groups_unknowns_last_and_updates_after_github_check(browser, live, width, tmp_path):
    login = f"project-visibility-{width}"
    live.add_user(login)
    projects = {
        "alpha-private": {"github": None},
        "beta-unknown": {"github": {"state": "unknown"}},
        "delta-public": {"github": {"state": "missing"}},
        "gamma-public": {"github": {"state": "current", "behind": 0, "ahead": 0}},
        "legacy": {},
        "local": {"git": "not_git", "github": None},
        "offline": {"git": "error", "github": None},
        "pending": {"github": None, "github_pending": True},
        "zeta-private": {"github": None},
    }
    machine = FakeMachine({name: {"summary": {"git": "ok", "checked_at": T0, **summary}}
                           for name, summary in projects.items()})
    machine.project_summary = True
    _, server = code_online(live, login, machine)
    gate = f"""(() => {{
      const real = window.fetch;
      window.remoteGate = new Promise(resolve => window.releaseRemote = resolve);
      window.fetch = async (url, options) => {{
        if (String(url).includes('/{pid('pending')}/summary?remote=true')) {{
          await window.remoteGate;
          return new Response(JSON.stringify({{git:'ok',checked_at:{T0},github:{{state:'current',behind:0,ahead:0}}}}),
            {{status:200,headers:{{'content-type':'application/json'}}}});
        }}
        return real(url, options);
      }};
      return true;
    }})()"""
    order = "[...document.querySelectorAll('#code-projects li')].map(row => row.dataset.pid)"
    ready = "!document.querySelector('#code-project-refresh')?.disabled && document.querySelector('#code-project-updated')?.textContent.startsWith('Обновлено:')"
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "gate", gate),
            *go("code"), ("wait", ready), set_value("#code-project-sort", "public"),
            ("click", "#code-project-refresh"), ("wait", ready), ("eval", "public", order),
            set_value("#code-project-sort", "private"), ("eval", "private", order),
            ("eval", "release", "window.releaseRemote(); true"),
            ("wait", f"document.querySelector(\"li[data-pid='{pid('pending')}'] .code-project-github\")?.textContent === 'GitHub ✓'"),
            ("eval", "resolvedPrivate", order),
            set_value("#code-project-sort", "public"), ("eval", "resolvedPublic", order),
            ("screenshot", str(tmp_path / f"project-visibility-{width}.png")),
            ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    public = ["delta-public", "gamma-public"]
    private = ["alpha-private", "zeta-private"]
    unknown = ["beta-unknown", "legacy", "local", "offline"]
    for key, names in (("public", public + private + unknown + ["pending"]),
                       ("private", private + public + unknown + ["pending"]),
                       ("resolvedPrivate", private + public + ["pending"] + unknown),
                       ("resolvedPublic", public + ["pending"] + private + unknown)):
        assert result["values"][key] == [pid(name) for name in names], result["values"]
    assert not result["values"]["overflow"] and not problems(result), problems(result)


@pytest.mark.parametrize("storage", ["invalid", "unavailable"])
def test_project_sort_remains_usable_with_bad_or_unavailable_storage(browser, live, storage):
    login = f"project-sort-storage-{storage}"
    live.add_user(login)
    machine = FakeMachine({name: {"summary": {"ideas": count, "git": "ok"}}
                           for name, count in (("alpha", 1), ("beta", 3))})
    machine.project_summary = True
    _, server = code_online(live, login, machine)
    init = """(() => {
      const read = Storage.prototype.getItem, write = Storage.prototype.setItem;
      Storage.prototype.getItem = function(key) {
        if (key.startsWith('studio-code-project-sort:')) {
          if (MODE === 'invalid') return 'unknown-sort';
          throw new DOMException('Storage blocked', 'SecurityError');
        }
        return read.call(this, key);
      };
      Storage.prototype.setItem = function(key, value) {
        if (MODE === 'unavailable' && key.startsWith('studio-code-project-sort:'))
          throw new DOMException('Storage blocked', 'SecurityError');
        return write.call(this, key, value);
      };
    })();""".replace('MODE', repr(storage))
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"),
            ("wait", "document.querySelector('#code-project-updated')?.textContent.startsWith('Обновлено:')"),
            ("eval", "fallback", "document.querySelector('#code-project-sort').value"),
            set_value("#code-project-sort", "ideas"),
            ("eval", "selected", "[document.querySelector('#code-project-sort').value, document.querySelector('#code-projects li').dataset.pid]")],
            init=init)
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["fallback"] == "name"
    assert result["values"]["selected"] == ["ideas", pid("beta")]
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_project_summaries_sort_refresh_and_stay_visible_while_scrolling(browser, live, width, tmp_path):
    login = f"project-summary-{width}"
    live.add_user(login)
    machine = FakeMachine({name: {} for name in ("alpha", "beta", "gamma", "delta")})
    for index, (name, project) in enumerate(machine.projects.items()):
        project["summary"] = {"findings": [1, 9, 2, 0][index], "ideas": [0, 1, 8, 2][index],
            "changed": index, "unpushed": [0, 0, 0, 3][index], "modified": T0 + index,
            "checked_at": T0, "git": "ok", "github": [
                {"state": "behind", "behind": 3, "ahead": 0},
                {"state": "current", "behind": 0, "ahead": 0},
                {"state": "ahead", "behind": 0, "ahead": 1},
                {"state": "diverged", "behind": 2, "ahead": 1}][index]}
    machine.projects.update({f"project-{n:02}": {} for n in range(35)})
    machine.projects["alpha"]["summary"]["remotes"] = [
        {"name": "origin", "state": "current", "behind": 0, "ahead": 0},
        {"name": "github", "state": "current", "behind": 0, "ahead": 0},
        {"name": "upstream", "state": "behind", "behind": 10, "ahead": 0},
        {"name": "cld", "state": "behind", "behind": 2, "ahead": 0}]
    machine.projects["project-00"]["summary"] = {"github": {"state": "missing"}}
    machine.project_summary = True
    aid, server = code_online(live, login, machine)
    first = "document.querySelector('#code-projects li').dataset.pid"
    order = lambda key: ("eval", "sort-" + key, f"(() => {{ const s = document.querySelector('#code-project-sort');"
        f" s.value = '{key}'; s.dispatchEvent(new Event('change')); return {first}; }})()")
    geometry = """(() => {
      const button = document.querySelector('#code-project-refresh'), r = button.getBoundingClientRect();
      const footer = document.querySelector('.code-panel-footer').getBoundingClientRect();
      const body = document.querySelector('.code-panel-body').getBoundingClientRect();
      return [r.top, r.bottom <= innerHeight && r.top >= 0 && body.bottom <= footer.top + 1
        && button.contains(document.elementFromPoint(r.x + r.width/2, r.y + r.height/2))];
    })()"""
    alpha = f"#code-projects li[data-pid='{pid('alpha')}']"
    gate = f"""(() => {{ const real = window.fetch; window.projectReads = 0;
      window.fetch = async (url, options) => {{
        const path = new URL(String(url), location.href).pathname;
        if (path.endsWith('/projects') && window.projectListGate) await window.projectListGate;
        if (path.endsWith('/summary')) {{
          window.projectReads++;
          if (window.projectGate) await window.projectGate;
          if (window.overrideSummary && String(url).includes('/{pid('alpha')}/'))
            return new Response(JSON.stringify({{findings:20,ideas:0,changed:7,unpushed:4,
              modified:{T0 + 10},checked_at:{T0 + 10},git:'ok',github:{{state:'unknown'}}}}),
              {{status:200,headers:{{'content-type':'application/json'}}}});
        }}
        return real(url, options);
      }}; return true; }})()"""
    loading = """(() => {
      const button = document.querySelector('#code-project-refresh'), footer = button.closest('.code-panel-footer');
      return [button.disabled, button.getAttribute('aria-busy'), footer.getAttribute('aria-busy'),
        getComputedStyle(button.firstElementChild).animationName, document.querySelector('#code-project-updated').textContent];
    })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "gate", gate),
            *go("code"), ("wait", "document.querySelector('#code-project-updated')?.textContent.startsWith('Обновлено:')"),
            ("click", "#code-project-refresh"),
            ("wait", "!document.querySelector('#code-project-refresh').disabled && window.projectReads>=39"),
            ("eval", "initialReadCount", "window.initialProjectReads=window.projectReads"),
            ("eval", "counts", f"document.querySelector(\"{alpha}\").textContent"),
            ("eval", "compactControls", """(() => {
              const select = document.querySelector('#code-project-sort'), sort = select.closest('label').getBoundingClientRect();
              const label = document.querySelector('.code-project-sort-text');
              const button = document.querySelector('#code-project-refresh'), refresh = button.getBoundingClientRect();
              const updated = document.querySelector('#code-project-updated').getBoundingClientRect();
              return Math.abs(sort.top - refresh.top) < 1 && sort.right < refresh.left
                && sort.height <= 34 && refresh.height <= 34 && refresh.width <= 34
                && parseFloat(getComputedStyle(label).fontSize) <= 11
                && (!matchMedia('(pointer: coarse)').matches || parseFloat(getComputedStyle(select).fontSize) >= 16)
                && button.scrollWidth <= button.clientWidth
                && updated.top >= Math.max(sort.bottom, refresh.bottom);
            })()"""),
            ("eval", "twoLines", """[...document.querySelectorAll('#code-projects .code-project')].every(row => {
              const heading = row.querySelector('.code-project-heading').getBoundingClientRect();
              const name = row.querySelector('.code-project-name').getBoundingClientRect();
              const metrics = row.querySelector('.code-project-metrics').getBoundingClientRect();
              const summary = row.querySelector('.code-project-summary').getBoundingClientRect();
              const git = row.querySelector('.code-project-git').getBoundingClientRect();
              const gh = row.querySelector('.code-project-github')?.getBoundingClientRect();
              const remotes = [...row.querySelectorAll('.code-project-remote')].map(e => e.getBoundingClientRect());
              const date = row.querySelector('.code-project-modified').getBoundingClientRect();
              const lineHeight = parseFloat(getComputedStyle(row.querySelector('.code-project-summary')).lineHeight);
              return metrics.left >= name.right && metrics.top < heading.bottom
                && summary.top >= heading.bottom && Math.abs(git.top - date.top) < 1
                && (!gh || gh.left >= git.right && gh.right <= summary.right && Math.abs(gh.top - git.top) < 1)
                && remotes.every(r => r.right <= summary.right && Math.abs(r.top - git.top) < 1)
                && summary.height <= lineHeight + 1 && row.children.length === 2;
            })"""),
            ("eval", "github", """[...document.querySelectorAll('.code-project-github')].map(e =>
              [e.textContent, e.dataset.gitLevel, e.title])"""),
            ("eval", "remotes", """[...document.querySelectorAll('.code-project-remote')].map(e =>
              [e.textContent, e.dataset.gitLevel, e.title])"""),
            ("screenshot", str(tmp_path / f"github-{width}.png")),
            order("findings"), order("ideas"), order("modified"), order("unpushed"),
            ("eval", "initial-unpushed", first), order("name"),
            ("eval", "before", geometry),
            ("eval", "scroll", "(() => {const b=document.querySelector('.code-panel-body');b.scrollTop=b.scrollHeight;return true;})()"),
            ("eval", "after", geometry),
            ("screenshot", str(tmp_path / f"projects-{width}.png")),
            ("eval", "hold", "window.projectGate = new Promise(resolve=>window.releaseProjects=resolve); window.projectListGate = new Promise(resolve=>window.releaseProjectList=resolve); true"),
            ("click", "#code-project-refresh"),
            ("wait", "document.querySelector('#code-project-refresh').getAttribute('aria-busy') === 'true'"),
            ("eval", "loadingList", loading),
            ("eval", "releaseList", "window.projectListGate=null; window.releaseProjectList(); true"),
            ("wait", "document.querySelector('#code-project-refresh').disabled && window.projectReads > window.initialProjectReads"),
            ("eval", "loadingSummary", loading),
            ("screenshot", str(tmp_path / f"projects-refresh-{width}.png")),
            ("eval", "previous", f"document.querySelector(\"{alpha} .code-project-findings\").textContent"),
            ("eval", "release", "window.overrideSummary=true; window.projectGate=null; window.releaseProjects(); true"),
            ("wait", "!document.querySelector('#code-project-refresh').disabled"),
            ("eval", "finished", loading),
            ("eval", "fresh", f"document.querySelector(\"{alpha}\").textContent"), order("unpushed"),
            ("eval", "final-unpushed", first),
            ("eval", "reads", "window.projectReads"),
            ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert [values["sort-" + key] for key in ("findings", "ideas", "modified", "name")] == [
        pid("beta"), pid("gamma"), pid("delta"), pid("alpha")]
    assert values["initial-unpushed"] == pid("delta")
    assert values["final-unpushed"] == pid("alpha"), {key: values[key] for key in ('fresh', 'reads', 'final-unpushed')}
    assert values["before"][1] and values["after"][1]
    assert abs(values["before"][0] - values["after"][0]) < 1
    assert "(П: 1 · И: 0)" in values["counts"] and "Git: 0 изм. ↑0" in values["counts"]
    assert values["previous"] == "П: 1" and "П: 20" in values["fresh"]
    assert "Git: 7 изм. ↑4" in values["fresh"] and values["reads"] == values["initialReadCount"] + 39
    assert values["twoLines"] and values["compactControls"]
    assert values["loadingList"] == values["loadingSummary"] == [True, 'true', 'true', 'spin', 'Обновляю сведения…']
    assert values["finished"][:4] == [False, 'false', 'false', 'none']
    assert values["finished"][4].startswith('Обновлено:')
    assert [entry[:2] for entry in values["github"]] == [
        ["GitHub ↓3", "red"], ["GitHub ✓", "green"], ["GitHub ↕", "red"],
        ["GitHub ↑1", "yellow"], ["GitHub ∅", "red"]]
    assert "отстаёт на 3" in values["github"][0][2] and "GitHub ?" in values["fresh"]
    assert [entry[:2] for entry in values["remotes"]] == [["cld ↓2", "red"]]
    assert "По локальным данным Git, без fetch" in values["remotes"][0][2]
    assert "null" not in values["counts"] and "undefined" not in values["counts"]
    assert not values["overflow"] and not problems(result), problems(result)


def test_project_refresh_preserves_data_on_errors_and_ignores_previous_computer(browser, live):
    login = "project-summary-errors"
    live.add_user(login)
    machine = FakeMachine({"alpha": {"summary": {"findings": 5, "ideas": 2, "changed": 1, "unpushed": 0,
        "modified": T0, "checked_at": T0, "git": "ok", "github": {"state": "behind", "behind": 4, "ahead": 0}}}})
    other = FakeMachine({"beta": {"summary": {"findings": 7, "ideas": 3, "changed": 2, "unpushed": 1,
        "modified": T0, "checked_at": T0, "git": "ok"}}})
    machine.project_summary = other.project_summary = True
    machine.projects["alpha"]["summary"]["remotes"] = [
        {"name": "cld", "state": "behind", "behind": 2, "ahead": 0}]
    aid, server = code_online(live, login, machine)
    bid, second = code_online(live, login, other, name="Второй ПК")
    gate = f"""(() => {{const real = window.fetch; window.heldSummaries = 0;
      window.fetch = async (url, options) => {{
        const path = new URL(String(url), location.href).pathname;
        if (String(url).includes('/agents/{aid}/')) {{
          if ((window.failList && path.endsWith('/projects'))
            || (window.failSummary && path.endsWith('/summary'))) throw new Error('test network failure');
          if (window.holdSummary && path.endsWith('/summary')) {{
            window.heldSummaries++;
            await new Promise(resolve=>window.releaseSummary=resolve);
            window.heldSummaries--;
          }}
        }}
        return real(url, options);
      }};return true;}})()"""
    ready = "!document.querySelector('#code-project-refresh')?.disabled && document.querySelector('#code-projects')"
    count = "document.querySelector('.code-project-findings').textContent"
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "gate", gate),
            ("eval", "open", f"location.hash='#/code/{aid}';true"),
            ("wait", ready), ("eval", "initial", count),
            ("eval", "last", "window.lastUpdated = document.querySelector('#code-project-updated').textContent"),
            ("eval", "fail", "window.failSummary=true"), ("click", "#code-project-refresh"),
            ("wait", "document.querySelector('#code-panel-status').textContent.includes('Не удалось')"),
            ("eval", "retained", count),
            ("eval", "errorFinished", "document.querySelector('#code-project-refresh').getAttribute('aria-busy') === 'false'"),
            ("eval", "staleGithub", "[document.querySelector('.code-project-github').textContent, document.querySelector('.code-project-github').dataset.gitLevel]"),
            ("eval", "staleRemote", "[document.querySelector('.code-project-remote').textContent, document.querySelector('.code-project-remote').dataset.gitLevel]"),
            ("eval", "unchanged", "window.lastUpdated === document.querySelector('#code-project-updated').textContent"),
            ("eval", "failList", "window.failSummary=false;window.failList=true;true"),
            ("click", "#code-project-refresh"), ("wait", ready), ("eval", "afterListFailure", count),
            ("eval", "listErrorFinished", "document.querySelector('#code-project-refresh').getAttribute('aria-busy') === 'false'"),
            ("eval", "retry", "window.failList=false;window.holdSummary=true;true"),
            ("click", "#code-project-refresh"), ("wait", "window.heldSummaries === 1"),
            ("click", f"#code-agents li[data-id='{bid}'] button"), ("wait", ready),
            ("eval", "other", count),
            ("eval", "release", "window.holdSummary=false;window.releaseSummary();true"),
            ("wait", "window.heldSummaries === 0"), ("eval", "keptOther", count),
            ("eval", "otherFinished", "document.querySelector('#code-project-refresh').getAttribute('aria-busy') === 'false'"),
            ("eval", "stillOther", f"!!document.querySelector('#code-projects li[data-pid=\"{pid('beta')}\"]')")])
    finally:
        second.stop()
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["initial"] == values["retained"] == values["afterListFailure"] == "П: 5"
    assert values["unchanged"] and values["stillOther"]
    assert values["errorFinished"] and values["listErrorFinished"] and values["otherFinished"]
    assert values["staleGithub"] == ["GitHub ↓4", "unknown"]
    assert values["staleRemote"] == ["cld ↓2", "unknown"]
    assert values["other"] == values["keptOther"] == "П: 7"
    assert not problems(result), problems(result)


def test_project_refresh_finishes_when_the_same_computer_is_chosen_again(browser, live):
    """Пока сводки грузятся, нажатие на тот же компьютер перечитывает проекты: прежнее обновление отменяется,
    ↻ не остаётся занятым, сводки дочитываются заново."""
    login = "project-summary-same-computer"
    live.add_user(login)
    machine = FakeMachine({"alpha": {"summary": {"findings": 5, "ideas": 2, "changed": 1, "unpushed": 0,
        "modified": T0, "checked_at": T0, "git": "ok"}}})
    machine.project_summary = True
    aid, server = code_online(live, login, machine)
    gate = """(() => {const real = window.fetch; window.heldSummaries = 0;
      window.fetch = async (url, options) => {
        if (window.holdOnce && new URL(String(url), location.href).pathname.endsWith('/summary')) {
          window.holdOnce = false; window.heldSummaries++;
          await new Promise(resolve => window.releaseSummary = resolve);
        }
        return real(url, options);
      };return true;})()"""
    ready = "!document.querySelector('#code-project-refresh')?.disabled && document.querySelector('#code-projects')"
    busy = "document.querySelector('#code-project-refresh').getAttribute('aria-busy')"
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "gate", gate),
            ("eval", "open", f"location.hash='#/code/{aid}';true"), ("wait", ready),
            ("eval", "hold", "window.holdOnce = true"), ("click", "#code-project-refresh"),
            ("wait", "window.heldSummaries === 1"),
            ("click", f"#code-agents li[data-id='{aid}'] button"),
            ("wait", f"{busy} === 'false' && document.querySelector('.code-project-findings')?.textContent === 'П: 5'"),
            ("eval", "release", "window.releaseSummary(); true"),
            ("wait", ready), ("eval", "busy", busy),
            ("eval", "label", "document.querySelector('#code-project-updated').textContent")])
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["busy"] == "false" and values["label"].startswith("Обновлено:"), values
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_project_counters_open_journals_and_git_with_unsaved_changes_guard(browser, live, width):
    login = f"project-links-{width}"
    live.add_user(login)
    machine = FakeMachine({"alpha": {"files": {"FINDINGS.md": b"# Findings\nProblem document\n",
        "IDEAS.md": b"# Ideas\nIdea document\n"}}, "beta": {}})
    machine.projects["beta"]["summary"] = {"github": {"state": "behind", "behind": 3, "ahead": 0}}
    machine.projects["beta"]["summary"]["remotes"] = [{"name": "cld", "state": "unknown"}]
    machine.project_summary = True
    aid, server = code_online(live, login, machine)
    alpha = f"#code-projects li[data-pid='{pid('alpha')}']"
    beta = f"#code-projects li[data-pid='{pid('beta')}']"
    panel = [("eval", "panel", """(() => { const toggle = document.querySelector('#code-panel-toggle');
      if (getComputedStyle(toggle).display !== 'none') toggle.click(); return true; })()"""),
      ("wait", "document.querySelector('#code-panel').getBoundingClientRect().left >= 0")]
    def blank(name, line):
        return ("eval", "blank-" + name + "-" + line, f"""(() => {{
          const row = document.querySelector('#code-projects li[data-pid="{pid(name)}"] .code-project');
          const r = row.getBoundingClientRect(), line = row.querySelector('.code-project-{line}').getBoundingClientRect();
          const target = document.elementFromPoint(r.right - 4, line.top + line.height / 2);
          const blank = target?.closest('.code-project') === row && !target.closest('a');
          if (blank) target.dispatchEvent(new MouseEvent('click', {{bubbles:true,cancelable:true}}));
          return blank; }})()""")
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"),
            ("wait", "document.querySelector('#code-project-updated')?.textContent.startsWith('Обновлено:')"),
            ("eval", "validLinks", "document.querySelectorAll('#code-projects a a').length === 0"),
            blank('beta', 'heading'), ("wait", "document.querySelector('.code-place-project')?.textContent === 'beta'"),
            *panel, blank('alpha', 'summary'), ("wait", "document.querySelector('.code-place-project')?.textContent === 'alpha'"),
            *panel,
            ("click", alpha + " .code-project-findings"),
            ("wait", "document.querySelector('#code-editor')?.value.includes('Problem document')"),
            ("eval", "findings", "[location.hash, document.querySelector('#code-path').textContent]"),
            *panel, ("click", alpha + " .code-project-findings"),
            *panel, ("click", alpha + " .code-project-ideas"),
            ("wait", "document.querySelector('#code-editor')?.value.includes('Idea document')"),
            set_value("#code-editor", "Unsaved journal draft"),
            *panel, blank('beta', 'summary'), ("wait", "document.querySelector('#code-unsaved')?.open"),
            ("press", "Escape"),
            *panel, ("click", beta + " .code-project-github"),
            ("wait", "document.querySelector('#code-unsaved')?.open"),
            ("press", "Escape"),
            *panel, ("click", beta + " .code-project-remote"),
            ("wait", "document.querySelector('#code-unsaved')?.open"),
            ("press", "Escape"), ("eval", "retained", "document.querySelector('#code-editor').value"),
            *panel, ("click", beta + " .code-project-git"),
            ("wait", "document.querySelector('#code-unsaved')?.open"),
            ("click", "#code-unsaved-leave"), ("wait", "!!document.querySelector('#code-git')"),
            ("eval", "git", "[location.hash, document.querySelector('.code-place-project').textContent]"),
            ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth")], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["validLinks"] and values["fits"]
    assert all(values[key] for key in ('blank-beta-heading', 'blank-alpha-summary', 'blank-beta-summary'))
    assert values["findings"][0].endswith('?tab=files&journal=findings') and 'FINDINGS.md' in values["findings"][1]
    assert values["retained"] == "Unsaved journal draft"
    assert values["git"] == [f"#/code/{aid}/{pid('beta')}?tab=git", "beta"]
    assert [r["path"] for r in code_requests(machine, "read")] == ["FINDINGS.md", "FINDINGS.md", "IDEAS.md"]
    assert not code_requests(machine, "save") and not code_requests(machine, "turn")
    assert not problems(result), problems(result)
