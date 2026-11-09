"""Accepted POST loss and failed stream reconciliation in the real browser."""
import pytest

from test_ui_browser import ANSWERED, READY, attach, browser, live, go, open_new_chat, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

LOSS = """(() => {
  const real=window.fetch; window.lost=0;
  window.fetch=async(...args)=>{
    const response=await real(...args);
    if (!window.lost && String(args[0]).endsWith('/turns')) {
      window.lost++; await response.arrayBuffer(); throw new TypeError('Synthetic reset');
    }
    return response;
  }; return true;
})()"""


def test_lost_accepted_post_is_reconciled_without_resending(browser, live):
    before = len(live.gw.chat_requests)
    result = browser.drive(live.url+'/', [*signin(), ('wait', READY), *open_new_chat(),
        ('eval', 'loss', LOSS), ('type', '#chat-input', 'Acknowledged once'), ('click', '#send'),
        ('wait', "window.lost===1 && document.querySelectorAll('#messages .msg').length===2 && document.querySelector('#chat-input').value===''") ,
        ('eval', 'messages', "[...document.querySelectorAll('#messages .msg .md, #messages .msg.user .bubble')].map(n=>n.textContent.trim())")])
    assert not result.get('failed'), result
    assert len(live.gw.chat_requests) - before == 1
    assert result['values']['messages'] == ['Acknowledged once', 'Здравствуйте!']


def test_manual_retry_reuses_key_when_reconciliation_is_unavailable(browser, live):
    before = len(live.gw.chat_requests)
    gate = """(() => {
      const real=window.fetch; window.denied=0;
      window.fetch=(url,options={})=>{
        if (/\\/turns\\//.test(String(url))) { window.denied++; return Promise.reject(new TypeError('No receipt yet')); }
        return real(url,options);
      }; return true;
    })()"""
    result = browser.drive(live.url+'/', [*signin(), ('wait', READY), *open_new_chat(),
        ('eval', 'loss', LOSS), ('eval', 'gate', gate), ('type', '#chat-input', 'Retry safely'), ('click', '#send'),
        ('wait', "window.lost===1 && document.querySelector('#chat-error').textContent.includes('Нет связи')"),
        ('click', '#send'), ('wait', ANSWERED),
        ('eval', 'users', "document.querySelectorAll('#messages .msg.user').length")])
    assert not result.get('failed'), result
    assert result['values']['users'] == 1
    assert len(live.gw.chat_requests) - before == 1


def test_failed_history_read_retries_after_stream_ends_and_keeps_new_draft(browser, live):
    before = len(live.gw.chat_requests)
    gate = """(() => {
      const real=window.fetch; window.denied=0;
      window.fetch=async(url,options={})=>{
        if (/^\\/api\\/chats\\/[^/]+$/.test(String(url)) && (options.method||'GET')==='GET' && window.denied<2) {
          window.denied++; throw new TypeError('History temporarily unavailable');
        }
        const response=await real(url,options);
        if (!String(url).endsWith('/turns')) return response;
        const text=(await response.text()).replace(/event: done[\\s\\S]*$/, ''); let sent=false;
        const stream=new ReadableStream({pull(controller){
          if(sent) controller.error(new TypeError('Lost end of stream'));
          else {sent=true;controller.enqueue(new TextEncoder().encode(text));}
        }});
        return new Response(stream,{headers:response.headers});
      }; return true;
    })()"""
    result = browser.drive(live.url+'/', [*signin(), ('wait', READY), *open_new_chat(),
        ('eval', 'gate', gate), ('type', '#chat-input', 'Recover answer'), ('click', '#send'),
        ('wait', "window.denied>=1"), ('type', '#chat-input', 'Next unsent question'),
        ('eval', 'online', "window.dispatchEvent(new Event('online')); true"),
        ('wait', "window.denied===2 && document.querySelector('#chat-error').textContent==='' && document.querySelectorAll('#messages .msg').length===2"),
        ('eval', 'draft', "document.querySelector('#chat-input').value")])
    assert not result.get('failed'), result
    assert result['values']['draft'] == 'Next unsent question'
    assert len(live.gw.chat_requests) - before == 1


@pytest.mark.parametrize('width', [320, 1280])
def test_switching_chats_while_sending_clears_the_sent_text_and_chips(browser, live, width):
    """Отправил текст с документом в А и сразу открыл Б: ответ POST пришёл после смены диалога — отправленное
    уходит из поля и в Б, своё набранное в Б остаётся."""
    hold = """(() => {
      const real=window.fetch;
      window.fetch=async(url,options={})=>{
        if (!window.held && String(url).endsWith('/turns')) {
          window.held=true;
          await new Promise(resolve=>window.releaseTurn=resolve);
        }
        return real(url,options);
      }; return true;
    })()"""
    second = """(async()=>{const {api}=await import('/static/js/api.js');
      window.second=(await api('POST','/api/chats',{})).id;return true;})()"""
    chips = "document.querySelectorAll('#composer .att-chip').length"
    phone = width == 320
    result = browser.drive(live.url+'/', [*signin(), ('wait', READY), *open_new_chat(mobile=phone),
        ('eval', 'second', second), ('eval', 'hold', hold),
        attach("[new File(['Документ для вопроса'], 'note.txt', {type: 'text/plain'})]"),
        ('wait', f"{chips}===1"),
        ('type', '#chat-input', 'Sent from A'), ('click', '#send'),
        ('wait', "typeof window.releaseTurn==='function'"),
        ('eval', 'switch', "location.hash='#/chat/'+window.second; true"),
        ('wait', "location.hash==='#/chat/'+window.second && document.querySelectorAll('#messages .msg').length===0"),
        ('eval', 'release', "window.releaseTurn(); true"),
        ('wait', f"document.querySelector('#chat-input').value==='' && {chips}===0"),
        ('type', '#chat-input', 'Draft of B'),
        ('eval', 'state', f"[document.querySelector('#chat-input').value, {chips}]")], width=width, mobile=phone)
    assert not result.get('failed'), result
    assert result['values']['state'] == ['Draft of B', 0]


@pytest.mark.parametrize('width', [320, 1280])
def test_busy_card_shows_the_wait_and_do_not_wait_ends_it(browser, live, width):
    """Шлюз отвечает 503: под ответом «жду карту · м:сс» и «Не ждать»; нажатие — ответ-ошибка словами, и они же
    после перечитывания диалога."""
    phone = width == 320
    live.gw.chat_error = (503, {'error': {'message': 'slot busy'}})
    last = "[...document.querySelectorAll('#messages .msg.assistant')].pop()"
    try:
        result = browser.drive(live.url+'/', [*signin(), ('wait', READY), *open_new_chat(mobile=phone),
            ('type', '#chat-input', 'Wait for the card'), ('click', '#send'),
            ('wait', f"{last}?.querySelector('.msg-meta').textContent.startsWith('жду карту · ')"),
            ('wait', f"!{last}.querySelector('.msg-nowait').hidden"),
            ('eval', 'overflow', 'document.documentElement.scrollWidth <= document.documentElement.clientWidth'),
            ('click', '#messages .msg.assistant .msg-nowait'),
            ('wait', f"{last}.querySelector('.msg-meta').textContent === 'карта занята — ждать не стали'"),
            ('eval', 'hidden', f"{last}.querySelector('.msg-nowait').hidden"),
            ('eval', 'reload', 'location.reload(); true'),
            ('wait', f"document.querySelectorAll('#messages .msg').length === 2 && {last}.querySelector('.msg-meta').textContent === 'карта занята — ждать не стали'")],
            width=width, mobile=phone)
    finally:
        live.gw.chat_error = None
    assert not result.get('failed'), result
    assert result['values']['overflow'] is True and result['values']['hidden'] is True


def test_late_recovery_does_not_replace_another_chat_or_its_draft(browser, live):
    hold = """(() => {
      const real=window.fetch;
      window.fetch=async(url,options={})=>{
        const response=await real(url,options);
        if (!window.firstId && String(url).endsWith('/turns')) {
          window.firstId=String(url).split('/')[3]; await response.arrayBuffer();
          throw new TypeError('Lost accepted response');
        }
        if (String(url)==='/api/chats/'+window.firstId && (options.method||'GET')==='GET')
          return new Promise(resolve=>window.releaseHistory=()=>resolve(response));
        return response;
      }; return true;
    })()"""
    second = """(async()=>{const {api}=await import('/static/js/api.js');
      window.second=(await api('POST','/api/chats',{})).id;return true;})()"""
    result = browser.drive(live.url+'/', [*signin(), ('wait', READY), *open_new_chat(),
        ('eval', 'second', second), ('eval', 'hold', hold), ('type', '#chat-input', 'First chat'), ('click', '#send'),
        ('wait', "typeof window.releaseHistory==='function'"),
        ('eval', 'switch', "location.hash='#/chat/'+window.second; true"),
        ('wait', "document.querySelector('#chat-error').textContent==='' && document.querySelectorAll('#messages .msg').length===0"),
        ('eval', 'replaceDraft', "document.querySelector('#chat-input').value=''; true"),
        ('type', '#chat-input', 'Other unsent draft'),
        ('eval', 'release', "window.releaseHistory(); true"), ('idle',),
        ('eval', 'state', "[location.hash==='#/chat/'+window.second,document.querySelector('#chat-input').value,document.querySelectorAll('#messages .msg').length]")])
    assert not result.get('failed'), result
    assert result['values']['state'] == [True, 'Other unsent draft', 0]
