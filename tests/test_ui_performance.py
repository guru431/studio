"""Управляемые медленные ответы: сначала полезный снимок, затем свежие проверки."""
import pytest

from fake_bridge import FakeMachine, T0, pid
from test_ui_browser import READY, browser, live, code_online, code_tab_ready, go, signin, problems, CODE_MSGS

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]
A = '11111111-1111-4111-8111-111111111111'
B = '22222222-2222-4222-8222-222222222222'


def session(title):
    return {'title': title, 'created': T0, 'modified': T0, 'messages': [
        {'id': 'm', 'role': 'assistant', 'text': title, 'steps': [], 'time': T0}]}


def test_cross_project_cached_text_does_not_wait_for_catalog_but_sending_does(browser, live):
    login = 'perf-cross-project'
    live.add_user(login)
    machine = FakeMachine({'alpha': {'chats': {A: session('Alpha text')}},
                           'beta': {'chats': {B: session('Beta text')}}})
    aid, bridge = code_online(live, login, machine)
    alpha, beta = f'code/{aid}/{pid("alpha")}/{A}', f'code/{aid}/{pid("beta")}/{B}'
    gate = f"""(() => {{
      const original = window.fetch;
      window.__hold = false; window.__turns = 0;
      window.fetch = (url, options = {{}}) => {{
        const path = new URL(String(url), location.href).pathname;
        if (path.endsWith('/turn')) window.__turns++;
        if (window.__hold && path === '/api/code/agents/{aid}/projects/{pid('alpha')}/chats')
          return new Promise(resolve => {{ window.__release = () => resolve(original(url, options)); }});
        return original(url, options);
      }};
      return true;
    }})()"""
    try:
        result = browser.drive(live.url+'/', [*signin(login), ('wait', READY), ('eval', 'gate', gate),
            *go(alpha), code_tab_ready(A), *go(beta), code_tab_ready(B),
            ('eval', 'hold', 'window.__hold=true; true'), *go(alpha),
            ('wait', "typeof window.__release==='function' && document.querySelector('#code-feed .md')?.textContent.trim()==='Alpha text'"),
            ('eval', 'waiting', "document.querySelector('#code-chat').dataset.loaded!=='1' && document.querySelector('#code-send').disabled"),
            ('eval', 'enter', "document.querySelector('#code-text').value='Do not send'; document.querySelector('#code-text').closest('form').requestSubmit(); true"),
            ('eval', 'noTurn', 'window.__turns===0'),
            ('eval', 'release', 'window.__hold=false; window.__release(); true'), code_tab_ready(A),
            ('eval', 'messages', CODE_MSGS)], width=1280)
    finally:
        bridge.stop()
    assert not result.get('failed'), result
    assert result['values']['waiting'] and result['values']['noTurn']
    assert result['values']['messages'] == [['assistant', 'Alpha text']]
    assert not problems(result), problems(result)


def test_completed_summary_appears_before_slow_project_and_is_reused_on_return(browser, live):
    login = 'perf-summary'
    live.add_user(login)
    machine = FakeMachine({'alpha': {}, 'beta': {}})
    machine.project_summary = True
    _, bridge = code_online(live, login, machine)
    gate = f"""(() => {{
      const original=window.fetch; window.__summaries=0; let held=false;
      window.fetch=(url, options) => {{
        const path=new URL(String(url),location.href).pathname;
        if (path.endsWith('/summary')) {{
          window.__summaries++;
          if (!held && path.includes('/{pid('beta')}/')) {{ held=true;
            return new Promise(resolve=>window.__release=()=>resolve(original(url,options))); }}
        }}
        return original(url,options);
      }}; return true;
    }})()"""
    try:
        result = browser.drive(live.url+'/', [*signin(login), ('wait', READY), ('eval', 'gate', gate), *go('code'),
            ('wait', f"document.querySelector('#code-projects li[data-pid=\"{pid('alpha')}\"] .code-project-findings')?.textContent==='П: 0' && typeof window.__release==='function'"),
            ('eval', 'stillLoading', "document.querySelector('#code-project-refresh').disabled"),
            ('eval', 'release', 'window.__release(); true'),
            ('wait', "document.querySelector('#code-project-updated')?.textContent.startsWith('Обновлено:')"),
            ('eval', 'before', 'window.__summaries'), *go('photo'), ('wait', "!!document.querySelector('#photo-feed')"),
            *go('code'), ('wait', "document.querySelectorAll('#code-projects li').length===2"),
            ('eval', 'after', 'window.__summaries'), ('eval', 'cached', "document.querySelector('.code-project-findings').textContent")], width=1280)
    finally:
        bridge.stop()
    assert not result.get('failed'), result
    assert result['values']['stillLoading'] and result['values']['before'] == result['values']['after'] == 2
    assert result['values']['cached'] == 'П: 0'
    assert not problems(result), problems(result)


def test_hidden_mobile_panel_does_not_read_other_projects(browser, live):
    login='perf-hidden-panel'
    live.add_user(login)
    machine=FakeMachine({**{'alpha': {'chats': {A: session('Only selected chat')}}},
                         **{f'project-{i:02d}': {} for i in range(40)}})
    machine.project_summary=True
    aid, bridge=code_online(live,login,machine)
    try:
        result=browser.drive(live.url+'/', [*signin(login),('wait',READY),
            *go(f'code/{aid}/{pid("alpha")}/{A}'), code_tab_ready(A),
            ('eval','panelHidden',"document.querySelector('#code-panel').getBoundingClientRect().right<=0")],width=320,mobile=True)
    finally:
        bridge.stop()
    assert not result.get('failed'),result
    assert result['values']['panelHidden']
    assert not [r for r in machine.requests if r['op']=='project_summary']
    assert not problems(result),problems(result)


def test_module_alias_is_single_instance_and_settings_share_agent_read(browser,live):
    login='perf-imports-settings'
    live.add_user(login)
    gate="""(() => { const original=window.fetch; window.__agents=0;
      window.fetch=(url,options)=>{if(String(url)==='/api/agents')window.__agents++;return original(url,options)};
      return true; })()"""
    imports="""(async()=>{
      const map=JSON.parse(document.querySelector('script[type=importmap]').textContent);
      const alias=await import('/static/js/api.js'), versioned=await import(map.imports['/static/js/']+'api.js');
      return alias===versioned && !!alias.me && map.imports['/static/js/'].startsWith('/static/v/');
    })()"""
    result=browser.drive(live.url+'/',[*signin(login),('wait',READY),('eval','imports',imports),
        ('eval','gate',gate),*go('settings/code'),('wait',"!!document.querySelector('#code-defaults-save') && !!document.querySelector('#agents')"),
        ('eval','reads','window.__agents')])
    assert not result.get('failed'),result
    assert result['values']['imports'] and result['values']['reads']==1
    assert not problems(result),problems(result)


def test_failed_parameter_check_keeps_sending_disabled_until_verified_retry(browser,live):
    login='perf-state-error'
    live.add_user(login)
    machine=FakeMachine({'alpha':{'chats':{A:session('History remains readable')}}})
    aid,bridge=code_online(live,login,machine)
    gate="""(() => {const original=window.fetch;window.__deny=true;window.__turns=0;
      window.fetch=(url,options)=>{const path=new URL(String(url),location.href).pathname;
        if(path.endsWith('/turn'))window.__turns++;
        if(window.__deny&&path.endsWith('/state'))return Promise.resolve(new Response(JSON.stringify({detail:{code:'timeout',message:'Параметры недоступны'}}),{status:504,headers:{'content-type':'application/json'}}));
        return original(url,options);};return true;})()"""
    try:
        result=browser.drive(live.url+'/',[*signin(login),('wait',READY),('eval','gate',gate),
            *go(f'code/{aid}/{pid("alpha")}/{A}'),code_tab_ready(A),
            ('eval','blocked',"document.querySelector('#code-send').disabled && document.querySelector('#code-feed').textContent.includes('History remains readable')"),
            ('eval','enter',"document.querySelector('#code-text').value='Do not send';document.querySelector('#code-text').closest('form').requestSubmit();true"),
            ('eval','noTurn','window.__turns===0'),
            ('eval','retry',"window.__deny=false;const model=document.querySelector('#code-model');model.value='opus';model.dispatchEvent(new Event('change'));true"),
            ('wait',"!document.querySelector('#code-send').disabled"),
            ('eval','cleared',"!document.querySelector('#code-status').textContent.includes('Параметры недоступны')")])
    finally:
        bridge.stop()
    assert not result.get('failed'),result
    assert all(result['values'][key] for key in ('blocked','noTurn','cleared'))
    assert not [row for row in machine.requests if row['op']=='turn']


def test_changed_engine_rebuilds_cached_chat_without_duplicate_forms(browser,live):
    from test_codex import CATALOG
    login='perf-engine-change'
    live.add_user(login)
    machine=FakeMachine({'alpha':{'chats':{A:session('Alpha')}},'beta':{'chats':{B:session('Beta')}}})
    machine.codex=CATALOG
    read=machine._op_chats
    reads=[]
    def change(req,events):
        if req['project']==pid('alpha'):
            reads.append(1)
            if len(reads)==2:machine.projects['alpha']['chats'][A]['engine']='codex'
        return read(req,events)
    machine._op_chats=change
    aid,bridge=code_online(live,login,machine)
    try:
        result=browser.drive(live.url+'/',[*signin(login),('wait',READY),
            *go(f'code/{aid}/{pid("alpha")}/{A}'),code_tab_ready(A),
            *go(f'code/{aid}/{pid("beta")}/{B}'),code_tab_ready(B),
            *go(f'code/{aid}/{pid("alpha")}/{A}'),code_tab_ready(A),
            ('wait',"document.querySelector('#code-chat').dataset.engine==='codex'"),
            ('eval','forms',"[document.querySelectorAll('#code-send').length,document.querySelectorAll('#code-feed').length]")])
    finally:
        bridge.stop()
    assert not result.get('failed'),result
    assert result['values']['forms']==[1,1]
    assert not problems(result),problems(result)


def test_partial_catalog_does_not_guess_engine_of_unknown_session(browser,live):
    login='perf-partial-catalog'
    live.add_user(login)
    machine=FakeMachine({'alpha':{'chats':{A:session('Known session')}}})
    aid,bridge=code_online(live,login,machine)
    gate="""(() => {const original=window.fetch;
      window.fetch=(url,options)=>new URL(String(url),location.href).pathname.endsWith('/chats')
        ?Promise.resolve(new Response(JSON.stringify({items:[],complete:false}),{status:200,headers:{'content-type':'application/json'}}))
        :original(url,options);return true;})()"""
    try:
        result=browser.drive(live.url+'/',[*signin(login),('wait',READY),('eval','gate',gate),
            *go(f'code/{aid}/{pid("alpha")}/{B}'),
            ('wait',"document.querySelector('#code-status')?.textContent.includes('Не удалось проверить движок')"),
            ('eval','noForm',"!document.querySelector('#code-send')")])
    finally:
        bridge.stop()
    assert not result.get('failed'),result
    assert result['values']['noForm']
    assert not [row for row in machine.requests if row['op'] in ('history','state','turn')]
