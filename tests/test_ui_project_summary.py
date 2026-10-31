"""Сведения о проектах, сортировка и закреплённое обновление в браузере."""

import pytest

from fake_bridge import FakeMachine, T0, pid
from test_ui_browser import READY, browser, code_online, go, live, problems, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize("width", [320, 1280])
def test_project_summaries_sort_refresh_and_stay_visible_while_scrolling(browser, live, width, tmp_path):
    login = f"project-summary-{width}"
    live.add_user(login)
    machine = FakeMachine({name: {} for name in ("alpha", "beta", "gamma", "delta")})
    for index, (name, project) in enumerate(machine.projects.items()):
        project["summary"] = {"findings": [1, 9, 2, 0][index], "ideas": [0, 1, 8, 2][index],
            "changed": index, "unpushed": [0, 0, 0, 3][index], "modified": T0 + index,
            "checked_at": T0, "git": "ok"}
    machine.projects.update({f"project-{n:02}": {} for n in range(35)})
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
        if (String(url).endsWith('/summary')) {{
          window.projectReads++;
          if (window.projectGate) await window.projectGate;
          if (window.overrideSummary && String(url).includes('/{pid('alpha')}/'))
            return new Response(JSON.stringify({{findings:20,ideas:0,changed:7,unpushed:4,
              modified:{T0 + 10},checked_at:{T0 + 10},git:'ok'}}),
              {{status:200,headers:{{'content-type':'application/json'}}}});
        }}
        return real(url, options);
      }}; return true; }})()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "gate", gate),
            *go("code"), ("wait", "document.querySelector('#code-project-updated')?.textContent.startsWith('Обновлено:')"),
            ("eval", "counts", f"document.querySelector(\"{alpha}\").textContent"),
            order("findings"), order("ideas"), order("modified"), order("unpushed"),
            ("eval", "initial-unpushed", first), order("name"),
            ("eval", "before", geometry),
            ("eval", "scroll", "(() => {const b=document.querySelector('.code-panel-body');b.scrollTop=b.scrollHeight;return true;})()"),
            ("eval", "after", geometry),
            ("screenshot", str(tmp_path / f"projects-{width}.png")),
            ("eval", "hold", "window.projectGate = new Promise(resolve=>window.releaseProjects=resolve); true"),
            ("click", "#code-project-refresh"),
            ("wait", "document.querySelector('#code-project-refresh').disabled && window.projectReads > 39"),
            ("eval", "previous", f"document.querySelector(\"{alpha} .code-project-findings\").textContent"),
            ("eval", "release", "window.overrideSummary=true; window.projectGate=null; window.releaseProjects(); true"),
            ("wait", "!document.querySelector('#code-project-refresh').disabled"),
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
    assert "Проблемы: 1" in values["counts"] and "Не запушено: 0" in values["counts"]
    assert values["previous"] == "Проблемы: 1" and "Проблемы: 20" in values["fresh"]
    assert "Изменено: 7" in values["fresh"] and values["reads"] == 78
    assert "null" not in values["counts"] and "undefined" not in values["counts"]
    assert not values["overflow"] and not problems(result), problems(result)


def test_project_refresh_preserves_data_on_errors_and_ignores_previous_computer(browser, live):
    login = "project-summary-errors"
    live.add_user(login)
    machine = FakeMachine({"alpha": {"summary": {"findings": 5, "ideas": 2, "changed": 1, "unpushed": 0,
        "modified": T0, "checked_at": T0, "git": "ok"}}})
    other = FakeMachine({"beta": {"summary": {"findings": 7, "ideas": 3, "changed": 2, "unpushed": 1,
        "modified": T0, "checked_at": T0, "git": "ok"}}})
    machine.project_summary = other.project_summary = True
    aid, server = code_online(live, login, machine)
    bid, second = code_online(live, login, other, name="Второй ПК")
    gate = f"""(() => {{const real = window.fetch; window.heldSummaries = 0;
      window.fetch = async (url, options) => {{
        if (String(url).includes('/agents/{aid}/')) {{
          if ((window.failList && String(url).endsWith('/projects'))
            || (window.failSummary && String(url).endsWith('/summary'))) throw new Error('test network failure');
          if (window.holdSummary && String(url).endsWith('/summary')) {{
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
            ("eval", "unchanged", "window.lastUpdated === document.querySelector('#code-project-updated').textContent"),
            ("eval", "failList", "window.failSummary=false;window.failList=true;true"),
            ("click", "#code-project-refresh"), ("wait", ready), ("eval", "afterListFailure", count),
            ("eval", "retry", "window.failList=false;window.holdSummary=true;true"),
            ("click", "#code-project-refresh"), ("wait", "window.heldSummaries === 1"),
            ("click", f"#code-agents li[data-id='{bid}'] button"), ("wait", ready),
            ("eval", "other", count),
            ("eval", "release", "window.holdSummary=false;window.releaseSummary();true"),
            ("wait", "window.heldSummaries === 0"), ("eval", "keptOther", count),
            ("eval", "stillOther", f"!!document.querySelector('#code-projects li[data-pid=\"{pid('beta')}\"]')")])
    finally:
        second.stop()
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["initial"] == values["retained"] == values["afterListFailure"] == "Проблемы: 5"
    assert values["unchanged"] and values["stillOther"]
    assert values["other"] == values["keptOther"] == "Проблемы: 7"
    assert not problems(result), problems(result)
