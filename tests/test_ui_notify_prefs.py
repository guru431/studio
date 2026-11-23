"""«О чём уведомлять» устройства (IDEAS 06:I6, 07:I5): виды, тихие часы и напоминание о карточке — сохранение и
вид после перезагрузки на узком и широком экране."""
import pytest

from test_ui_browser import READY, browser, go, live, problems, set_value, signin

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]

SAVED = "document.querySelector('#notify-prefs-status').textContent === 'Выбор уведомлений сохранён на этом устройстве'"
READY_PREFS = ("document.querySelector('#notify-prefs-save') && !document.querySelector('#notify-prefs-save').disabled"
               " && document.querySelector('#notify-zone').textContent !== ''")
FORM = ("[...document.querySelectorAll('#notify-prefs input[type=checkbox]')].map(b => b.checked)"
        ".concat([document.querySelector('#notify-quiet-from').value, document.querySelector('#notify-quiet-to').value,"
        " document.querySelector('#notify-quiet-from').disabled, document.querySelector('#notify-remind').value])")


@pytest.mark.parametrize('width', [320, 1280])
def test_notify_prefs_save_and_reload(browser, live, width, tmp_path):
    login = f'notify-prefs-{width}'
    live.add_user(login)
    result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('settings/notifications'),
        ('wait', READY_PREFS), ('eval', 'default', FORM),
        ('click', '#notify-kind-jobs'), ('click', '#notify-quiet'),
        set_value('#notify-quiet-from', '23:00'), set_value('#notify-quiet-to', '07:30'),
        set_value('#notify-remind', '10'), ('click', '#notify-prefs-save'), ('wait', SAVED),
        ('eval', 'saved', "fetch('/api/me/notify').then(r => r.json())"),
        ('eval', 'reload', 'location.reload(); true'), ('wait', READY), ('wait', READY_PREFS),
        ('eval', 'restored', FORM),
        ('screenshot', str(tmp_path / f'notify-prefs-{width}.png'), '#notify-prefs'),
        ('eval', 'overflow', 'document.documentElement.scrollWidth > innerWidth')],
        width=width, mobile=width == 320)
    assert not result.get('failed'), result
    values = result['values']
    assert values['default'] == [True, True, True, True, False, '22:00', '08:00', True, '30']
    assert values['saved'] == {'off': ['jobs'], 'quiet_from': '23:00', 'quiet_to': '07:30', 'remind': 10,
                               'timezone': 'Europe/Moscow'}
    assert values['restored'] == [True, True, False, True, True, '23:00', '07:30', False, '10']
    assert not values['overflow']
    assert not problems(result), problems(result)
