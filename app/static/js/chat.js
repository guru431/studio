import { locale, message, tr } from './i18n.js';
import { chooseDialogMcp, draftCount, draftOf, hasWebTools, stateOf } from './chat-mcp.js';
import { knowledgeChip } from './knowledge.js';
import { MEDIA_ACCEPT, byteSize, consentAsks, consentNode, draftIds, isMedia, mediaChip, mediaTargets, resultFiles,
  uploadMedia, resultPathLinks } from './chat-files.js';
import { paintPending } from './chat-pending.js';
import { paintSuggestions } from './chat-suggest.js';
import { contextRing } from './chat-context.js';
import { answerSteps, chatStatus } from './chat-status.js';
import { contextWindow } from './chat-context-window.js';
import { statsDialog } from './chat-stats.js';
import { filesDialog } from './chat-dialog-files.js';
import { chatTime, chatTitle } from './chat-title.js';
import { MS_MAX, REJECTED, usageBody, usageOf, usageRefused, withSpeed } from './chat-usage.js';
import { placeSummary } from './chat-summary.js';
import { chatSkills, humanText, skillMessage } from './chat-skills.js';
import { noteParts, partNode, partsButton } from './chat-parts.js';
import { mcpStateWords } from './mcp-sections.js';
/* Раздел «Чат» (#/chat — новый диалог, #/chat/<id> — диалог): список диалогов, чип модели,
 * лента сообщений с markdown, поле ввода с вложениями.
 *
 * Чип модели — отмеченные модели раздела «Чат» по подключениям (GET /api/sections), новый диалог —
 * с умолчанием раздела; списки моделей сервисов при открытии меню не запрашиваются.
 *
 * Ход: вложения — POST /api/attachments по одному, затем POST /api/chats/<id>/turns. Подключение
 * через сервер отвечает потоком SSE (turn, delta, done, error); прямое — JSON с адресом и
 * сообщениями: браузер зовёт модель сам с токеном из /api/connections/<id>/token (токен — только
 * в памяти вкладки) и кладёт итог PUT .../messages/<id> (у ошибки — её слова `error`); поток без [DONE] и finish_reason — ошибка
 * с прочитанным текстом, а не готовый ответ; длиннее ANSWER_MAX — как у сервера: дальше не читается,
 * обрезан с пометкой CUT_SIZE и сохранён ошибкой. «Остановить»: через сервер — POST .../stop,
 * прямое — обрыв запроса и PUT со статусом stopped.
 *
 * Ответы, которые читает эта вкладка (`jobs`), живут дольше экрана: ушли в другой диалог или
 * раздел — ответ дочитывается и сохраняется. Пока вкладка читает ответ из браузера, она раз в
 * BEAT_MS шлёт признак жизни (POST .../alive): сервер отдаёт такой ответ с `live` — другая вкладка
 * показывает его идущим и опрашивает диалог, как ответ сервера, который идёт без нас. Ответ
 * `streaming` из браузера без признака жизни (вкладку закрыли) — «прервано». Закрытие и перезагрузка вкладки
 * посреди ответа (`pagehide`) кладут прочитанное PUT с keepalive со статусом stopped — начало, что влезает в
 * предел тела keepalive (`partBody`); сетевой обрыв потока ждёт `pagehide` LEAVE_MS — перезагрузка рвёт поток раньше.
 * Неотправленное вложение крестик удаляет и с сервера (DELETE /api/attachments/<id>); неподходящий тип и размер
 * чип называет при выборе файла. Отправленное фото — миниатюра (окно с ним), документ — ссылка на скачивание.
 * Первый ход нового диалога не прошёл — созданный под него диалог удаляется (`dropEmpty`). Открытый диалог
 * перечитывается после ответа, при возвращении вкладки, нажатии на него в списке и на 409 `busy`.
 * Раздел без модели (ctx.hint): диалоги читаются, переименовываются и удаляются; новый диалог —
 * подсказка вместо поля ввода, в прежнем поле ввода есть (ход его пару не перепроверяет).
 *
 * Из расшифровки (#/chat?transcript=<id>&ask=…, спека 2026-10-21-studio-transcript-edit-chat-design,
 * раздел 9): новый диалог, POST /api/transcripts/<id>/attachments — вложение встаёт в поле уже
 * загруженным (`meta` без файла), в поле — вопрос по `ask` (набранный текст остаётся, вопрос — после него);
 * адрес — сразу #/chat без записи в историю.
 * Модель — локальная (`local_models` ссылки раздела): умолчание, иначе первая отмеченная, иначе не
 * выбрана, и «Отправить» закрыто до явного выбора. Ход и «Ответить заново», что унесут расшифровку
 * (вложением хода или из истории диалога — признак `external` сервера) внешнему получателю —
 * после окна «уйдёт во внешний сервис» (ход с `external_ok`;
 * 409 `external` сервера — то же окно). Ничего не отправляется само. У отправленного вложения из
 * расшифровки — ссылка «к записи».
 *
 * «Интернет» (спека 2026-10-21-studio-chat-internet, 6.8 и 6.10): кнопка #web-toggle рядом со скрепкой —
 * `web` диалога (PATCH {web}); у нового диалога — в памяти до создания, затем тем же PATCH. Видна, если
 * подключение идёт через сервер (`proxied` диалога; у нового — подключения из чипа); без ключа Exa
 * (`me.exa_key`, оболочка перечитывает профиль при возвращении на экран — событие `studio-me`) выключена, не
 * нажата и с подсказкой WEB_HINT — если набор MCP диалога не даёт веб-инструментов («интернет» по каталогу):
 * тогда кнопка включается ради них с подсказкой WEB_MCP_TITLE; 409 `no_exa_key` (ключ убрали в другой вкладке) —
 * его слова, кнопка выключена. Событие SSE `tool` — плашка над ответом с крутилкой («🌐 Ищу: …»), ответ с ходом
 * перечитывается: над ним плашка из `tools` (спека 2026-10-22-studio-chat-tools-badge-reasoning, раздел 3) —
 * виды и числа вызовов, зелёная без ошибок и красная с ними, нажатие раскрывает «Источники» под ответом —
 * ссылки в новую вкладку без адреса студии. Ход, чьи
 * запросы к Exa модель составит из расшифровки (`external_web` диалога или новое вложение из неё), — после
 * окна #web-dialog (ход с `web_ok`; 409 `external_web` — то же окно); окно внешней модели — первым.
 *
 * «Рассуждение» (спека 2026-10-22-studio-chat-tools-badge-reasoning, раздел 4): кнопка #think-toggle рядом с
 * «Интернетом» — `thinking` диалога (PATCH {thinking}; у нового — в памяти до создания, затем тем же PATCH).
 * Видна, где сервер её исполняет: `thinking_ok` диалога, у нового — модель из `local_models` подключения чипа
 * (sections.is_local); 409 `no_thinking` — его слова, кнопка прячется. Обе включены — под полем подсказка
 * THINK_NOTE (подсказка «нет ключа» важнее). Событие SSE `think` — модель думает: над ответом «Думает… N с»
 * (счётчик страницы — сумма отрезков от `think` до текста или инструмента; крутится что-то одно: инструмент на
 * плашке или рассуждение) и текст рассуждения, если сервер его шлёт; готовый ответ с `reasoning`
 * — свёрнутое «💭 Рассуждала N с» (`reasoning_sec`). Рассуждение — только текстом, без разметки.
 *
 * Сканы (спека 2026-10-27-studio-ocr, 6.5): ход через сервер ждёт распознавания там — событие SSE `ocr {page, pages}`
 * (своя вкладка) или `ocr` идущего ответа (вкладка, что его не читает) дают плашку «📄 Распознаю скан · стр. N из M»
 * до первого текста модели. Прямое подключение — 409 `not_ready`: под полем NOT_READY, страница опрашивает документы
 * сканов поля ввода и вложений диалога и повторяет ход. #/chat?ocr=<id>&ask=… — как из расшифровки,
 * POST /api/ocr/<id>/attachments, модель — умолчание раздела, без окна внешней модели.
 *
 * Роль диалога — чип roles.js, серверы и инструменты MCP — окно chat-mcp.js (чип #mcp-toggle `MCP (n)`): у нового
 * диалога — черновики `draftRole` и `draftMcp` (null — общие серверы по умолчанию), PATCH после его создания.
 * Согласие на передачу файлов диалога MCP, звук и видео диалога и файлы результатов MCP под ответом — chat-files.js;
 * звук нового диалога сначала создаёт диалог из черновика (`ensureChat`, как первый ход).
 *
 * Пауза ответа (спека 2026-11-21-studio-source-capabilities-design, п. 5 и 17): событие SSE `pending` или `pending`
 * сообщения с сервера — карточка chat-pending.js под ответом (подтверждение инструмента или вопрос модели); ответ —
 * POST /api/chats/<id>/pending/<id>, его поток SSE читает тот же пузырь (`answerPending`, `readServer`).
 *
 * Поле ввода (спека 2026-11-21-studio-source-capabilities-design, п. 2, 31, 33): подсказки под пустой заметкой —
 * chat-suggest.js; строка паспорта над полем (`paintPassport`): фото во вложениях, а в паспорте модели чипа нет
 * `vision` — «Убрать фото» и «Сменить модель», при MCP с `studio/chat-files` и согласием — какой сервер получит фото;
 * отправку не закрывает. Кольцо заполнения контекста открытого диалога — chat-context.js в `.composer-opts`.
 *
 * «Разговор» (п. 13): значок #talk-toggle в поле ввода над микрофоном — круг chat-voice.js, его состояние — строка
 * #talk-note под полем. Фраза уходит обычным ходом
 * (`voiceTurn` → `submit`), его ответ помечен `job.voice`: `notify` отдаёт текст ответа в озвучку по мере `delta`.
 * Другой диалог, уход из раздела и повторное нажатие круг останавливают.
 *
 * Из поиска (#/chat/<id>?m=<id сообщения>): лента открывается не с конца, а с этого сообщения (`found`, `reveal`) —
 * верх у верха ленты и вспышка рамки `.msg-found`; длинные ответы выше дорисовываются кадрами, и место держится,
 * пока человек не тронул ленту. В адресе — только id. */
import { api, me, unknownOutcome } from './api.js';
import { icon, iconButton } from './icons.js';
import { menuItem, popupMenu } from './menu.js';
import { calendarDay } from './time.js';
import { enterSends, fieldBox } from './field.js';
import { sectionDraft } from './drafts.js';
import { chatDrafts } from './chat-drafts.js';
import { messageNav } from './message-nav.js';
import { QUEUE_MAX, chatQueue } from './chat-queue.js';
import { fileDrop } from './chat-drop.js';
import { keep } from './feed.js';
import { kick } from './jobs.js';
import { renderMarkdown, renderMarkdownInto } from './markdown.js';
import { saveMedia } from './save.js';
import { CORS } from './settings.js';
import { readSSE } from './sse.js';
import { chatList } from './chat-list.js';
import { chooseProject, projectEditor, projectFilePicker, projectMemory, projectName, projectsPanel, PROJECTS_API } from './chat-projects.js';
import { chatTabs } from './chat-tabs.js';
import { findView } from './code-find.js';
import { button, el, field, focusDesktop, openDialog, plural, words } from './ui.js';
import { shareMenu } from './chat-share.js';
import { roleChooser } from './roles.js';
import { readAloud } from './speech-read.js';
import { startConversation } from './chat-voice.js';
import { dropPreviews, previewLinks } from './chat-preview.js';
import { continueOffer, lengthError, lengthRefusal } from './chat-continue.js';
import { knowledgeStep, sourceClicks } from './chat-knowledge-sources.js';
import { imageThumb, resultImages } from './chat-result-images.js';
import { effortChooser } from './chat-effort.js';
import { answerSignal } from './sound.js';
import { quoteChip, selectionQuote } from './quote.js';
import { messageMenu } from './message-menu.js';

const DOC_EXT = ['.pdf', '.docx', '.txt', '.md', '.csv', '.tsv', '.json', '.jsonl', '.xml', '.yaml', '.yml', '.toml',
  '.ini', '.log', '.py', '.js', '.ts', '.html', '.css', '.sql', '.sh', '.ps1'];   // как documents.SUFFIXES
const ACCEPT = [...DOC_EXT, 'image/*'].join(',');
const ATTACH_MAX = 10;           // как chat.ATTACH_MAX
const PHOTO_EXT = ['.png', '.jpg', '.jpeg', '.jfif', '.webp'];    // фото сервер узнаёт и по первым байтам (chat._is_photo)
const PHOTO_TYPES = ['image/png', 'image/jpeg', 'image/webp'];
const DOC_MAX = 20 * 1024 * 1024;   // как documents.DOC_MAX
const PHOTO_MAX = 10 * 1024 * 1024; // как image_upload.MAX_UPLOAD
const RENDER_MS = 100;           // markdown идущего ответа — не чаще
const THINK_TAIL = 600;          // знаков свёрнутого бегущего рассуждения: в три строки помещается меньше
const POLL_MS = 2000;
const RECOVERY_DELAYS = [1000, 2000, 4000, 8000, 15000];
const BEAT_MS = 15000;           // признак жизни ответа из браузера; сервер ждёт его 90 с (ALIVE_SEC)
const STICK_PX = 48;             // ближе к низу — автопрокрутка не отпускает ленту
const REVEAL_PX = 12;            // сообщение из поиска — на столько ниже верха ленты
const COPIED_MS = 1500;
const CUT_LENGTH = '\n\n[ответ обрезан пределом длины модели]';   // как chat.CUT_LENGTH сервера
const CUT_SIZE = '\n\n[ответ обрезан: слишком длинный для студии]';   // как chat.CUT_SIZE сервера
const ANSWER_MAX = 2000000;      // как chat.ANSWER_MAX: длиннее PUT ответа не примет
const ERROR_MAX = 400;           // как chat.ERROR_MAX: слова ошибки ответа в PUT
const ASKS = {                   // вопрос в поле диалога из расшифровки — на языке интерфейса
  discuss: '',
  summary: tr("Составь краткие итоги этой записи: о чём говорили и к чему пришли."),
  tasks: tr("Выдели из записи принятые решения и задачи: что сделать, кто отвечает, сроки, если названы."),
};
const SCAN_ASKS = {              // вопрос в поле диалога из «Распознавания»; перевод — на язык интерфейса
  discuss: '',
  summary: tr("Кратко перескажи этот документ: о чём он и что в нём главное."),
  translate: tr("Переведи этот документ на русский"),
};
const NOT_READY_MS = 3000;       // прямое подключение: опрос скана, затем ход заново
const SCAN_RUNNING = ['queued', 'working', 'cancelling'];   // как ocr.RUNNING
const NOT_READY = tr("скан ещё распознаётся — отправлю, когда будет готов");
const EXTERNAL_NOTE = tr("Запись уйдёт во внешний сервис — выберите модель");
const MCP_TEXT_CHAT = tr("Ответы через MCP доступны в текстовых диалогах без вложений и баз знаний");
const WEB_HINT = tr("Задайте ключ Exa в Настройках → Ввод и интернет → Интернет");
// chats.protected_used: в диалоге сработал «Защищённый источник» MCP — сервер не включит «Интернет» (409 `protected`)
const PROTECTED_HINT = tr("В этом диалоге сработал защищённый источник — «Интернет» здесь недоступен");
// флажок администратора «Веб и внешние MCP — только через Cleaner» (`me.cleaner_web`, cleaner_route.web_allowed):
// диалог не через Cleaner — сервер не даст ходу ни «Интернета», ни веб-инструментов MCP (PATCH — 409 `cleaner_web`)
const CLEANER_WEB_HINT = tr("Администратор разрешил «Интернет» только в диалогах через Cleaner");
// и «Поделиться», и экспорт такого диалога сервер отклонит (409 `protected`, chat.unprotected)
const PROTECTED_SHARE = tr("В этом диалоге сработал защищённый источник — им нельзя поделиться и его нельзя скачать");
const WEB_KINDS = ['search', 'page', 'youtube'];   // как web.KINDS: с ключом через сервер — все
const WEB_TITLE = tr("Интернет: поиск, страницы и субтитры YouTube");
// без ключа Exa: «Интернет» открывает только веб-инструменты MCP диалога (поиска Studio нет)
const WEB_MCP_TITLE = tr("Интернет: только веб-инструменты MCP — для поиска Studio задайте ключ Exa в Настройках");
const WEB_ASK = tr("Запросы, составленные из расшифровки, могут уйти в Exa — отправить?");
const TOOL_WORDS = { search: tr("Ищу"), page: tr("Читаю"), youtube: tr("Субтитры"), mcp: 'MCP', part: tr("Часть"),
  ask: tr("Вопрос модели"), todo: tr("План работы"), memory: tr("Память"), knowledge: tr("Знания") };   // строка хода по виду инструмента
// встроенные инструменты (chat_tools.KIND_OF): подпись события — имя функции, на плашке — только слово вида
const BUILTIN_KINDS = ['ask', 'todo', 'memory', 'knowledge'];
// значок идущего инструмента; прочие — интернет 🌐
const TOOL_MARKS = { youtube: '▶', mcp: '🧩', part: '📑', ask: '❓', todo: '📋', memory: '🧠', knowledge: '📚' };
const THINK_TITLE = tr("Рассуждение: модель думает перед ответом, ход мысли виден");
const THINK_NOTE = tr("С рассуждением модель реже ищет в интернете");   // замер 2026-10-22: 5 поисков из 10 против 10
const TALK_TITLE = tr("Разговор: говорите — модель ответит голосом, текст останется в диалоге");
// строка под полем по состоянию круга «Разговора» (chat-voice.js)
const TALK_WORDS = { listening: tr("Разговор: слушаю"), hearing: tr("Разговор: слышу речь"), thinking: tr("Разговор: жду ответа"),
  speaking: tr("Разговор: говорю") };

const SAVE_DELAYS = [1000, 2000, 4000, 8000];   // повторы PUT ответа прямого подключения после обрыва связи

const jobs = new Map();          // id диалога → ответ, который читает эта вкладка
const tokens = new Map();        // id подключения → { token, url } прямого подключения (память вкладки)
/** Ответы прямого подключения, которые не удалось сохранить: id → { chatId, message, body, error } — только память
 * вкладки; у ответа «Сохранить ещё раз», пока сервер держит его `streaming`. */
const unsaved = new Map();
let screen = null;               // смонтированный раздел
let lastChat = null;             // последний диалог в этой вкладке Студии
// перезагрузка потеряла бы ответ, который читает сама вкладка (прямое подключение, ещё не принятый ход), и
// несохранённые ответы — автоматическую (refresh.js) они запрещают; ответ через сервер идёт и без вкладки
document.addEventListener('studio-before-auto-refresh', (event) => {
  if (unsaved.size || [...jobs.values()].some((job) => job.message.via !== 'server')) event.preventDefault();
});

function elapsed(since) {
  const s = Math.max(0, Math.floor((Date.now() - since) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function groupOf(updated) {
  const days = calendarDay(Date.now() / 1000).number - calendarDay(updated).number;
  return days <= 0 ? tr("Сегодня") : days === 1 ? tr("Вчера") : tr("Ранее");
}

/** Начало text не длиннее max единиц UTF-16 и без половины эмодзи на краю: одиночный старший
 * суррогат PUT ответа не принял бы (422). */
function clip(text, max) {
  const cut = text.slice(0, max);
  return /[\uD800-\uDBFF]$/.test(cut) ? cut.slice(0, -1) : cut;
}

/** Без одиноких суррогатов: половину эмодзи (пара разорвана в потоке модели) PUT отклонил бы (422) — «�». */
function wellFormed(text) {
  return text.replace(/[\uD800-\uDBFF][\uDC00-\uDFFF]|[\uD800-\uDFFF]/g, (c) => (c.length === 2 ? c : '�'));
}

/** Итог ответа прямого подключения — PUT. Ответа нет (связь, срок, 502–504) — повтор с паузами SAVE_DELAYS: сервер
 * принимает PUT, пока ответ `streaming`, а тот же итог повторно — 200. `gone` — ответа в диалоге больше нет, сохранять
 * нечего. 409 `state` — ответ уже закрыт иначе (брошен по сроку или сохранён другой вкладкой): этот текст не сохранён
 * и не сохранится — «Сохранить ещё раз» нет (`closed`), текст остаётся на экране, чтобы его скопировать. Не вышло —
 * «Ответ не сохранился: …», текст остаётся в `unsaved`. true — сохранён. */
async function saveAnswer(chatId, message, body) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      await api('PUT', `/api/chats/${chatId}/messages/${message.id}`, body);
      break;
    } catch (err) {
      if (err.code === 'gone') break;
      const closed = err.code === 'state';
      if (!closed && attempt < SAVE_DELAYS.length && unknownOutcome(err)) {
        await new Promise((resolve) => { setTimeout(resolve, SAVE_DELAYS[attempt]); });
        continue;
      }
      if (!unsaved.has(message.id)) unsaved.set(message.id, { chatId, message, body, error: message.error });
      unsaved.get(message.id).closed = closed;
      message.status = 'error';                     // готовый, но не сохранённый — иначе строка под ним пуста
      message.error = closed ? tr("Ответ не сохранился: сервер уже закрыл его — скопируйте текст")
        : tr("Ответ не сохранился: {0}", words(err));
      return false;
    }
  }
  const kept = unsaved.get(message.id);
  if (kept) {
    unsaved.delete(message.id);
    message.status = body.status;
    message.error = kept.error;
  }
  return true;
}

function notify(job, final, replaced = false) {
  job.eventAt = Date.now();                        // строка состояния: «последнее событие N с назад»
  if (job.voice) {                                 // ход «Разговора» (chat-voice.js): текст ответа — в озвучку
    job.voice.update(job.message.content, final, replaced);
    if (final) job.voice.resolve(job.message.status);
  }
  if (screen) screen.refresh(job, final);
}

function finish(job) {
  if (jobs.get(job.chatId) === job) jobs.delete(job.chatId);
  notify(job, true);
  answerSignal(job.message, { quiet: !!job.voice });   // «Разговор» отвечает голосом — сигнал звучал бы поверх озвучки
}

/* ---- чтение ответа ------------------------------------------------------------ */

/* секунды рассуждения хода — сумма отрезков, как reasoning_sec сервера: идущий — от своего первого `think` */
function thinkSec(job) {
  return (job.thinkSum || 0) + (job.thinking ? (Date.now() - job.thinkFrom) / 1000 : 0);
}

/* модель кончила думать (пошёл текст ответа или инструмент, ответ кончился): секунды — счётчиком страницы до
   перечитывания */
function thinkEnd(job) {
  if (!job.thinking) return;
  job.thinkSum = thinkSec(job);
  job.thinking = false;
  job.message.reasoning_sec = job.thinkSum;
}

function append(job, text) {
  thinkEnd(job);
  job.first = true;
  job.firstAt ??= Date.now();      // начало текста — от него время генерации (`gen_ms`) прямого подключения
  job.tool = null;                 // модель снова пишет — инструмент кончился
  job.scan = null;                 // и скан распознан
  job.message.content += text;
  notify(job, false);
}

/* плашка скана: сервер шлёт `page` — страниц с итогом, а показываем ту, что распознаётся сейчас */
function scanLine({ page, pages }) {
  return tr("📄 Распознаю скан · стр. {0} из {1}", Math.min(page + 1, pages), pages);
}

/* строка хода инструмента: «Ищу: запрос»; подпись YouTube сервер начинает словом «субтитры» */
function toolLine({ kind, label }) {
  const what = TOOL_WORDS[kind] || tr("Инструмент");
  let text = BUILTIN_KINDS.includes(kind) ? '' : String(label || '');
  if (kind === 'youtube') text = text.replace(/^субтитры\s+/, '');
  return text ? `${what}: ${text}` : `${what}…`;
}

function adoptIds(job, data) {
  if (job.user) job.user.id = data.user_message_id;
  job.message.id = data.assistant_message_id;
}

async function readServer(job, response) {
  job.message.via = 'server';
  let cut = true;
  try {
    for await (const { event, data } of readSSE(response)) {
      if (event === 'turn') adoptIds(job, data);
      else if (event === 'delta' && data && typeof data.text === 'string') append(job, data.text);
      else if (event === 'replace' && data && typeof data.text === 'string') {
        job.message.content = data.text;
        job.first = true;
        notify(job, false, true);
      } else if (event === 'cleaner' && data && typeof data.raw === 'string') job.message.cleaner_raw = data.raw;
      else if (event === 'pending' && data && typeof data.id === 'string') {   // пауза: карточка — после `done`
        job.message.pending = data;
        notify(job, false);
      }
      else if (event === 'todo' && data && Array.isArray(data.items)) {   // план ответа (todo_write) — блок над ним
        job.message.todo = data.items;
        notify(job, false);
      }
      else if (event === 'ocr' && data && Number.isFinite(data.page) && Number.isFinite(data.pages)) {
        job.scan = data;                           // ответ ждёт распознавания скана — плашка до первого текста
        notify(job, false);
      } else if (event === 'wait' && data) {       // {seconds} — ждём карту (503 шлюза), {} — шлюз принял запрос
        job.scan = null;
        job.wait = Number.isFinite(data.seconds) ? Date.now() - data.seconds * 1000 : null;
        job.card = data.card || null;              // очередь карты (card.py): кто на ней и сколько примерно ждать
        notify(job, false);
      } else if (event === 'recovery' && data) {
        thinkEnd(job);
        job.tool = null;
        job.message.recovery = data.stage;
        notify(job, false);
      } else if (event === 'part' && data) {       // часть ответа (run_part): запуск и итог — кнопке «Части»
        noteParts(job.message, data);
        notify(job, false);
      } else if (event === 'closed') {             // сессия вышла или «Чат» заперт: перечитать через вход/блокировку
        break;
      } else if (event === 'tool' && data && typeof data.kind === 'string') {
        if (!data.skipped) {                       // skipped — MCP не ответил: шаг-ошибка без вызова, не крутить
          thinkEnd(job);                           // крутится одно: инструмент, а не «Думает…»
          job.scan = null;
          job.tool = data;
        }
        job.tooled = true;                         // по концу — перечитать: источники в ответе с сервера
        // плашка до перечитывания — без итогов шагов; запрос согласия на файлы MCP — сразу блоком под ответом
        (job.message.calls ||= []).push(data.consent ? { kind: data.kind, server: data.label, consent: data.consent }
          : data.kind === 'part' ? { kind: 'part', label: data.label } : { kind: data.kind });   // часть — с названием
        notify(job, false);
      } else if (event === 'think' && data) {      // {text} — ход мысли виден, {} — модель только думает
        job.tool = null;                           // модель снова думает — инструмент кончился
        job.scan = null;
        if (!job.thinking) job.thinkFrom = Date.now();
        job.thinking = true;
        if (typeof data.text === 'string') job.message.reasoning = (job.message.reasoning || '') + data.text;
        notify(job, false);
      } else if (event === 'done' || event === 'error') {
        job.message.recovery = null;
        job.message.status = event === 'done' ? data.status : 'error';
        if (event === 'error') job.message.error = data.message;
        if (event === 'done' && Number.isFinite(data.reasoning_sec)) job.doneSec = data.reasoning_sec;
        cut = false;
        break;
      }
    }
  } catch {
    // связь оборвалась — ответ на сервере идёт дальше, диалог перечитается
  }
  if (cut) job.lost = true;
  thinkEnd(job);
  // секунды, что записал сервер, — а не часы страницы: иначе «1 с» до перечитывания и «2 с» после
  if (job.doneSec !== undefined) job.thinkSum = job.message.reasoning_sec = job.doneSec;
  finish(job);
}

/* Токен — только тому адресу, для которого сервер его выдал: подключение сменило адрес (здесь,
 * в другой вкладке или у администратора) — прежний токен новому адресу не уходит. */
async function tokenFor(connId, url) {
  const known = tokens.get(connId);
  if (known && known.url === url) return known.token;
  tokens.delete(connId);
  const fresh = await api('GET', `/api/connections/${connId}/token`);
  if (fresh.chat_url !== url) throw new Error(tr("Адрес подключения сменился — отправьте сообщение ещё раз"));
  tokens.set(connId, { token: fresh.token, url });
  return fresh.token;
}

/* замены Cleaner по типам строкой («Люди: 2 · Почта: 1») — предпросмотр прежнего вида и «Что получила модель» */
function cleanerCounts(counts) {
  const typeNames = { PERSON: tr("Люди"), ORG: tr("Организации"), ADDRESS: tr("Адреса"), EMAIL: tr("Почта"), PHONE: tr("Телефоны"),
    BIRTHDATE: tr("Даты рождения"), PASSPORT: tr("Паспорта"), INN: tr("ИНН"), SNILS: tr("СНИЛС"), OGRN: tr("ОГРН"), KPP: tr("КПП"),
    OMS: tr("Полисы"), CARD: tr("Карты"), BANK_ACCOUNT: tr("Счета"), SECRET: tr("Секреты"), ACCOUNT: tr("Учётные записи"),
    IP: 'IP', MAC: 'MAC', HOST: tr("Узлы"), DOMAIN: tr("Домены"), SID: 'SID', DN: 'LDAP', TERM: tr("Термины") };
  return Object.entries(counts || {}).map(([type, count]) => `${typeNames[type] || type}: ${count}`).join(' · ');
}

/* 400 с именем отправленного параметра профиля подключения и признаком отказа — слова profiles.REFUSED, как у хода
   через сервер (profiles.refusal): одного имени мало — ошибка длины контекста vLLM называет `max_tokens` */
async function serviceRefusal(response, params) {
  let text = '';
  try {
    text = await response.text();
  } catch {
    // тело не прочиталось — без подробностей
  }
  const lowered = text.toLowerCase();
  const refused = response.status === 400 && REJECTED.some((marker) => lowered.includes(marker))
    && Object.keys(params || {}).find((name) => lowered.includes(name));
  // уровень рассуждения — выбор диалога, а не профиля (profiles.EFFORT_REFUSED)
  if (['reasoning_effort', 'reasoning'].includes(refused)) return tr("модель не принимает уровень рассуждения — выберите «Как у модели»");
  if (refused) return tr("подключение не принимает параметр «{0}» — уберите его в профиле подключения", refused);
  let detail = '';
  try {
    const error = JSON.parse(text).error;
    detail = String((error && error.message) || error || '').slice(0, 300);
  } catch {
    // не JSON — без подробностей
  }
  return tr("Сервис ответил ошибкой ({0}){1}", response.status, detail ? `: ${detail}` : '');
}

async function readBrowser(job, turn) {
  job.message.via = 'browser';
  adoptIds(job, turn);
  // признак жизни; ответ уже не идёт (диалог удалили, историю стёрли) — читать его дальше незачем
  const beat = setInterval(() => {
    api('POST', `/api/chats/${job.chatId}/messages/${job.message.id}/alive`).catch((err) => {
      if (err.code === 'gone' || err.code === 'state') job.controller.abort();
    });
  }, BEAT_MS);
  let status = 'done';
  let cut = false;                                  // запрос оборвала сеть — или уход страницы (`LEAVE_MS`)
  try {
    const token = await tokenFor(job.connId, turn.url);
    let response;
    // `usage` в потоке (chat-usage.js): сервис отверг параметр — один повтор без него, подключение запомнено
    const ask = () => fetch(turn.url, { method: 'POST', signal: job.controller.signal,
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...(turn.params || {}), ...usageBody(job.connId), model: turn.model, messages: turn.messages,
        stream: true }),
      credentials: 'omit', referrerPolicy: 'no-referrer', cache: 'no-store' });
    try {
      response = await ask();
      if (response.status === 400 && usageRefused(job.connId, 400, await response.clone().text())) response = await ask();
    } catch (err) {
      cut = err instanceof TypeError;
      throw cut ? new Error(CORS) : err;
    }
    if (response.status === 401 || response.status === 403) tokens.delete(job.connId);
    if (!response.ok) throw new Error(await serviceRefusal(response, turn.params));
    // конец ответа — [DONE] или finish_reason; поток без них (обрыв, HTML вместо потока, пустой
    // ответ) — ошибка, а не готовый ответ; `length` — обрезан пределом длины модели
    let ending = null;
    try {
      for await (const { data } of readSSE(response)) {
        if (data === '[DONE]') {
          ending ||= 'stop';
          break;
        }
        if (!data || typeof data !== 'object') continue;
        if (data.error) throw new Error(tr("Модель прервала ответ: {0}", String(data.error.message || data.error).slice(0, 300)));
        if (data.usage) job.usage = usageOf(data.usage) || job.usage;   // кусок `choices: []` перед [DONE]
        for (const choice of data.choices || []) {
          const text = choice && choice.delta && choice.delta.content;
          if (typeof text === 'string' && text) append(job, text);
          if (choice && typeof choice.finish_reason === 'string' && choice.finish_reason) ending = choice.finish_reason;
        }
        if (job.message.content.length > ANSWER_MAX) {     // как сервер: дальше не читать, прочитанное — ошибкой
          job.message.content = clip(job.message.content, ANSWER_MAX) + CUT_SIZE;
          throw new Error(tr("Ответ слишком длинный — обрезан"));
        }
      }
    } catch (err) {
      cut = err instanceof TypeError;
      throw cut ? new Error(tr("Связь с сервисом оборвалась")) : err;
    }
    if (!ending) throw new Error(tr("Модель оборвала ответ: поток кончился без признака конца"));
    if (ending === 'length') {
      job.message.content += CUT_LENGTH;
      throw new Error(tr("Ответ обрезан пределом длины модели"));
    }
  } catch (err) {
    if (err && err.name === 'AbortError') status = 'stopped';
    else {
      status = 'error';
      job.message.error = words(err);
    }
  }
  // Перезагрузка рвёт поток сетевой ошибкой за миг до `pagehide`: итог ошибки ждёт LEAVE_MS — успел `pagehide`,
  // и ответ сохранён «остановлен» с прочитанным (`job.parted`), а не ошибкой, которую сервер уже не сменил бы.
  if (cut) await new Promise((resolve) => { setTimeout(resolve, LEAVE_MS); });
  if (job.parted) {               // вкладку закрывали: итог уже ушёл keepalive — тот же итог сервер примет повторно
    status = 'stopped';
    job.message.content = job.parted.content;
  }
  job.message.status = status;
  job.message.content = wellFormed(job.message.content);
  // признак жизни — до конца сохранения: пока идут повторы PUT, другие вкладки не считают ответ брошенным
  // слова ошибки сервер хранит у ответа (`messages.error`): видны и после перечитывания диалога
  const error = status === 'error' ? clip(wellFormed(job.message.error || ''), ERROR_MAX) : '';
  // вкладку закрывали — итог тот же, что ушёл keepalive, с его числами: при закрытии дойдёт только он
  job.numbers = numbersOf(job);
  job.saving = job.parted || { content: job.message.content, status, ...(error ? { error } : {}), ...job.numbers };
  Object.assign(job.message, { usage: job.saving.usage || null, gen_ms: job.saving.gen_ms ?? null,
    total_ms: job.saving.total_ms });
  await saveAnswer(job.chatId, job.message, job.saving);
  clearInterval(beat);
  finish(job);
}

/* ---- закрытие вкладки посреди ответа прямого подключения ---------------------- */

/* Тело fetch с keepalive — не больше 64 КиБ на все такие запросы страницы разом; запас — прочим (jobs.js). */
const PART_BYTES = 60 * 1024;
/* Сколько итог сетевого обрыва ждёт `pagehide`: Chromium рвёт поток уходящей страницы при смене документа, за
 * миллисекунду до `pagehide`, как бы долго ни шла сама перезагрузка. */
const LEAVE_MS = 250;

const bodyBytes = (body) => new TextEncoder().encode(JSON.stringify(body)).length;

/** Токены и время ответа прямого подключения к этому мигу — серверу (решение 19 спеки 2026-11-25): целые от 0 до
 * chat.MS_MAX. */
function numbersOf(job) {
  const ended = Date.now();
  const ms = (since) => Math.min(MS_MAX, Math.max(0, Math.round(ended - since)));
  return { ...(job.usage ? { usage: job.usage } : {}), ...(job.firstAt ? { gen_ms: ms(job.firstAt) } : {}),
    total_ms: ms(job.started) };
}

/** Итог «остановлен» с прочитанным и числами ответа: начало ответа, а не хвост, — остановленный ответ всегда начало
 * сказанного моделью, и история с контекстом следующего хода читаются связно; длиннее limit байт тела — начало, что
 * влезает. */
function partBody(content, limit = PART_BYTES, numbers = {}) {
  const size = (text) => bodyBytes({ content: text, status: 'stopped', ...numbers });
  let text = wellFormed(clip(content, limit));                  // знак — не меньше байта
  if (size(text) > limit) {
    let low = 0;
    let high = text.length;
    while (low < high) {
      const mid = Math.ceil((low + high) / 2);
      if (size(clip(text, mid)) <= limit) low = mid;
      else high = mid - 1;
    }
    text = clip(text, low);
  }
  return { content: text, status: 'stopped', ...numbers };
}

/* Вкладку закрывают или перезагружают, пока она читает ответ прямого подключения: прочитанное — PUT с keepalive
 * (обычный запрос уход страницы оборвал бы), иначе текст пропал бы, а диалог ещё ALIVE_SEC отвечал бы «ответ идёт в
 * другой вкладке». Уже сохраняемый итог — тем же телом ещё раз (сервер примет тот же итог повторно), а не влезающий
 * в квоту — началом со статусом stopped. Квота keepalive одна на все ответы страницы: каждый следующий получает
 * остаток, ответ без остатка не шлётся (браузер отверг бы такой fetch молча). */
window.addEventListener('pagehide', () => {
  let left = PART_BYTES;
  for (const job of jobs.values()) {
    if (job.message.via !== 'browser' || !job.message.id) continue;
    let body = job.saving || job.parted;
    if (!body || bodyBytes(body) > left) {
      body = partBody((job.saving || job.message).content, left, job.saving ? job.numbers : numbersOf(job));
    }
    if (bodyBytes(body) <= left) {
      if (!job.saving) job.parted = body;
      left -= bodyBytes(body);
      api('PUT', `/api/chats/${job.chatId}/messages/${job.message.id}`, body, { keepalive: true, unlock: false })
        .catch(() => {});
    }
    job.controller.abort();
  }
});

/* ---- источники ответа («Интернет») --------------------------------------------- */

function isWebLink(url) {
  try {
    return ['http:', 'https:'].includes(new URL(url).protocol);
  } catch {
    return false;                                       // не адрес — текстом
  }
}

/* ссылка — только http(s) и в новую вкладку без Referer: сайты не узнают адрес студии */
function sourceLink(url, text) {
  const name = text || url || '';
  return isWebLink(url) ? el('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, name)
    : el('span', {}, name);
}

/* страница и ролик с ошибкой — адрес, который составила модель (и, может быть, отвергла студия), —
   текстом, не ссылкой */
function stepLink(s) {
  return s.error ? el('span', {}, s.title || s.url || '') : sourceLink(s.url, s.title);
}

/* «Источники (N)» — число ссылок (найденное поиском, страница, ролик), а не шагов */
function linkCount(steps) {
  const urls = steps.flatMap((s) => {
    if (s.kind === 'search') return (Array.isArray(s.results) ? s.results : []).map((x) => x && x.url);
    return s.error ? [] : [s.url];
  });
  return urls.filter(isWebLink).length;
}

function duration(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const rest = String(s % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${rest}` : `${m}:${rest}`;
}

function stepNode(s, { ended = false, sources = false } = {}) {
  if (s.kind === 'part') return partNode(s, { ended });   // часть ответа (chat-parts.js)
  const li = el('li', { class: `web-step web-${s.kind}` });
  if (s.kind === 'search') {
    li.append(s.query ? tr("Поиск: «{0}»", s.query) : tr("Поиск"));
    const results = Array.isArray(s.results) ? s.results : [];
    if (results.length) {
      li.append(el('ul', {}, ...results.map((x) => el('li', {}, sourceLink(x.url, x.title),
        x.published ? el('span', { class: 'muted' }, ` · ${x.published}`) : null))));
    }
  } else if (s.kind === 'page') li.append(tr("Страница: "), stepLink(s));
  else if (s.kind === 'youtube') {
    const about = [s.channel, s.duration ? duration(s.duration) : '', s.lang].filter(Boolean).join(' · ');
    li.append(tr("Ролик: "), stepLink(s), about ? el('span', { class: 'muted' }, ` · ${about}`) : null);
  } else if (s.kind === 'mcp') li.append(['MCP', s.server, s.tool].filter(Boolean).join(' · '));   // без tool — сервер не ответил
  else if (s.kind === 'ask') li.append(s.question ? tr("Вопрос: «{0}»", s.question) : tr("Вопрос модели"));
  else if (s.kind === 'knowledge') knowledgeStep(li, s, { buttons: sources });   // куски — кнопками под ответом
  else if (s.kind === 'duplicate') li.append(tr("Повтор пропущен: {0}", s.tool));   // chat.DUPLICATE
  else if (s.kind === 'todo') li.append(tr("План работы"));                        // chat_tools.TODO_WRITE
  else if (s.kind === 'memory') li.append(s.refused || s.error ? tr("Память")
    : s.action === 'forget' ? tr("Память: запись удалена") : tr("Память: запись добавлена"));
  else li.append(tr("Инструмент"));
  if (s.error) li.append(el('span', { class: 'error' }, ` — ${message(s.error)}`));
  else if (s.note) li.append(el('span', { class: 'muted web-note-step' }, ` — ${message(s.note)}`));   // mcp_client.TRIMMED
  else if (s.refused) li.append(el('span', { class: 'muted' }, ` — ${tr("Вызов отклонён")}`));
  return li;
}

const count = (n, forms) => `${n} ${plural(n, forms)}`;

/* план ответа (`todo_write`, `messages.todo`): «План: N из M» и шаги с отметкой состояния; тот же план не
   перерисовывается (`p.planKey`) — опрос и дельты текста его не пересоздают */
const TODO_STATES = { done: tr("сделано"), in_progress: tr("в работе"), pending: tr("впереди") };

function paintTodo(p, items) {
  const list = Array.isArray(items) ? items.filter((i) => i && typeof i.text === 'string' && TODO_STATES[i.status]) : [];
  const key = JSON.stringify(list);
  if (key === p.planKey) return;
  p.planKey = key;
  if (!list.length) {
    p.plan.replaceChildren();
    return;
  }
  const done = list.filter((i) => i.status === 'done').length;
  p.plan.replaceChildren(el('div', { class: 'chat-todo', role: 'group', 'aria-label': tr("План работы") },
    el('p', { class: 'chat-todo-head' }, tr("План: {0} из {1}", done, list.length)),
    el('ol', {}, ...list.map((i) => el('li', { class: `todo-${i.status}` },
      el('span', { class: 'sr' }, `${TODO_STATES[i.status]}: `), i.text)))));
}

/* плашка над ответом: «🌐 Интернет (Exa): 2 поиска, 1 страница · ▶ YouTube: 1 ролик — 1 ошибка»; YouTube идёт
   не через Exa — своей частью, вызов несуществующего инструмента — «неверный вызов» */
function badgeText(steps) {
  const of = (kind) => steps.filter((s) => s.kind === kind).length;
  const exa = [];
  if (of('search')) exa.push(count(of('search'), [tr("поиск"), tr("поиска"), tr("поисков")]));
  if (of('page')) exa.push(count(of('page'), [tr("страница"), tr("страницы"), tr("страниц")]));
  const parts = exa.length ? [tr("🌐 Интернет (Exa): {0}", exa.join(', '))] : [];
  if (of('youtube')) parts.push(`▶ YouTube: ${count(of('youtube'), [tr("ролик"), tr("ролика"), tr("роликов")])}`);
  // заметка MCP (`note`: список урезан пределом) — не вызов и не ошибка
  const notes = steps.filter((s) => s.kind === 'mcp' && s.note && !s.error).length;
  if (of('mcp') > notes) parts.push(`MCP: ${of('mcp') - notes}`);
  if (notes) parts.push(count(notes, [tr("заметка MCP"), tr("заметки MCP"), tr("заметок MCP")]));
  if (of('ask')) parts.push(count(of('ask'), [tr("вопрос"), tr("вопроса"), tr("вопросов")]));   // ask_user (chat-pending.js)
  if (of('knowledge')) parts.push(tr("Знания: {0}", count(of('knowledge'), [tr("поиск"), tr("поиска"), tr("поисков")])));
  if (of('duplicate')) parts.push(count(of('duplicate'), [tr("повтор пропущен"), tr("повтора пропущено"), tr("повторов пропущено")]));
  if (of('todo')) parts.push(tr("план"));
  if (of('memory')) parts.push(tr("память: {0}", of('memory')));
  if (of('part')) parts.push(count(of('part'), [tr("часть"), tr("части"), tr("частей")]));   // run_part (chat-parts.js)
  const wrong = steps.filter((s) => !WEB_KINDS.includes(s.kind) && !['mcp', 'ask', 'knowledge', 'duplicate', 'todo', 'memory', 'part'].includes(s.kind)).length;
  if (wrong) parts.push(count(wrong, [tr("неверный вызов"), tr("неверных вызова"), tr("неверных вызовов")]));
  const errors = steps.filter((s) => s.error).length;
  return parts.join(' · ') + (errors ? ` — ${count(errors, [tr("ошибка"), tr("ошибки"), tr("ошибок")])}` : '');
}

/* ссылок нет (одни вызовы MCP, ошибки) — заголовок считает вызовы, а не «Источники (0)»; шаг автоматического
   блока «Знаний» (`auto`) — в списке, но не вызов: при одних таких шагах заголовок считает их куски */
function sourcesNode(steps, ended) {
  const links = linkCount(steps);
  const calls = steps.filter((s) => !s.auto).length;
  const pieces = steps.reduce((n, s) => n + (s.auto && Array.isArray(s.hits) ? s.hits.length : 0), 0);
  return el('details', { class: 'web-steps' },
    el('summary', {}, links ? tr("Источники ({0})", links) : calls ? tr("Вызовы инструментов ({0})", calls)
      : tr("Источники ({0})", pieces)),
    el('ul', { class: 'web-list' }, ...steps.map((s) => stepNode(s, { ended, sources: true }))));
}

/* ---- экран -------------------------------------------------------------------- */

function build(view, ctx) {
  let chatId;                  // показанный диалог; null — новый, ещё не созданный
  let projects = [];
  let projectId = null;
  let draftProject = null;
  let chat = null;             // { chat, messages } с сервера
  let mcpModelServers = [];   // servers that expose MCP model completions
  let links = [];              // подключения раздела «Чат» с отмеченными моделями (GET /api/sections)
  let chatDefault = null;      // { connection_id, model } — умолчание раздела
  let draft = null;            // { connection_id, model } нового диалога
  let atts = [];               // вложения поля ввода: { file, meta, error, busy, transcript }; из расшифровки — без file
  let needModel = false;       // диалог из расшифровки без локальной модели: «Отправить» — после явного выбора
  let draftWeb = false;        // «Интернет» нового диалога: PATCH после его создания
  let draftMcp = null;         // MCP нового диалога (тело PATCH, chat-mcp.js); null — общие серверы по умолчанию
  let mcpServers = [];         // серверы MCP учётки (GET /api/mcp): счёт `MCP (n)` нового диалога
  let catalog = [];            // каталог MCP учётки (GET /api/mcp/catalog): `chat_files` — звук и видео в поле ввода
  let mcpStatus = {};          // {sid: {state, queue, message}} серверов со `studio/status` — точка у чипа MCP
  let pendingMedia = [];       // звук и видео, что грузятся или не приняты: { name, mime, size, busy, error }
  let creating = null;         // идущее создание нового диалога (ensureChat)
  let draftThink = false;      // «Рассуждение» нового диалога — так же
  let draftRole = null;        // роль нового диалога {id, name}: PATCH {role_id} после его создания
  let draftEffort = null;      // уровень рассуждения нового диалога (chat-effort.js): PATCH {effort} после его создания
  let draftKbs = [];           // базы знаний нового диалога (чип «Знания»): PUT …/knowledge после его создания
  let sending = false;
  let stick = true;
  let found = null;            // { id, lit } сообщение из поиска (?m=): держим на виду, пока человек не тронул ленту
  let opened = 0;              // медленный ответ прежнего диалога не перебьёт новый
  let poll = null;
  let notReady = null;         // повтор хода прямого подключения, пока скан распознаётся (409 `not_ready`)
  let paintTimer = null;
  let lastPaint = 0;
  let alive = true;
  let needsReload = false;
  let failures = 0;
  let historyRead = null;
  let pendingTurn = null;      // request id and unsent draft: this view's memory only
  let pendingAnswer = null;    // { signature, requestId } ответа на паузу: повтор того же ответа — с тем же id
  let editing = null;         // выбранное сообщение и снимок конца диалога
  let droppedNote = '';        // адрес подключения чипа убран из списка администратора — слова для #model-note
  let listed = [];             // диалоги списка (GET /api/chats): сколько ещё на удалённом подключении
  let moveFor = null;          // диалог, под чипом которого стоит предложение #chat-move
  let prefill = null;          // { id, text } — текст в поле диалога после его загрузки (openWith)
  const parts = new WeakMap(); // сообщение → его узлы в ленте
  const received = new WeakMap(); // сообщение с сервера → его JSON при получении (settle)
  const shown = new WeakMap();    // сообщение → { node, last, cleaner }: узел в ленте и с чем он нарисован
  const unfolded = new Set();  // id ответов, чьё рассуждение человек раскрыл: лента перестраивается целиком

  /* разметка */
  const list = el('nav', { class: 'chat-list', 'aria-label': tr("Диалоги") });
  const fresh = el('button', { type: 'button', class: 'btn chat-new', id: 'chat-new' }, icon('plus'), tr("Новый чат"));
  const projectsUI = projectsPanel({
    chats: el('div', { class: 'project-chats' }, fresh, list),
    select: (id) => ctx.navigate(`chat?p=${id}`),
    create: () => projectEditor(root, null, (item) => {
      projects = [...projects.slice(0, 1), item, ...projects.slice(1)];
      ctx.navigate(`chat?p=${item.id}`);
    }),
    edit: (item) => projectEditor(root, item, (saved) => {
      projects = projects.map((p) => p.id === saved.id ? saved : p);
      paintProjects();
    }),
    memory: (item) => projectMemory(root, item),
  });
  const aside = el('aside', { id: 'chats', class: 'chats' }, projectsUI.node);
  const backdrop = el('div', { class: 'chats-backdrop', hidden: true });
  const toggle = iconButton('list', tr("Диалоги"), { id: 'chats-toggle', class: 'icon-btn chats-toggle', 'aria-controls': 'chats',
    'aria-expanded': 'false' });
  const chipName = el('span', { class: 'chip-name' });
  // чип модели — первым в ряду под полем, как выбор модели «Агентов»; подключение — подсказкой чипа и для скринридера
  const chip = el('button', { type: 'button', id: 'model-chip', class: 'model-chip ctl-chip' },
    el('span', { class: 'dot', 'aria-hidden': 'true' }), chipName, icon('down', 'ico caret'));
  // меню моделей — общий компонент menu.js: группы по подключениям, выбранная — menuitemradio с ✓
  const modelMenu = el('div', { id: 'model-menu', class: 'model-menu', role: 'menu', 'aria-label': tr("Модель") });
  const models = popupMenu(chip, modelMenu, { fill: fillModels, side: 'above' });
  const connName = el('span', { class: 'chat-conn sr' });
  const modelWrap = el('div', { class: 'model-wrap' }, chip, connName, modelMenu);
  // диалог через шлюз Cleaner (`routed`, cleaner_route.routes): признак рядом с выбором модели
  const routedTag = el('span', { class: 'tag server chat-routed', id: 'chat-routed', hidden: true, title: tr("Диалог идёт через Cleaner") },
    el('span', { 'aria-hidden': 'true' }, 'Cleaner'), el('span', { class: 'sr' }, tr("Диалог идёт через Cleaner")));
  // название в шапке, действия с открытым диалогом над полем ввода (chat-title.js)
  const title = chatTitle({ rename: renameDialog, remove: deleteDialog, move: moveProject, pinItem,
    said: (text) => { notice.textContent = text; } });
  const head = el('header', { class: 'head chat-head' }, toggle, title.label, title.note);
  const modelNote = el('p', { class: 'chat-model-note error', id: 'model-note', 'aria-live': 'polite' });
  const feed = el('div', { class: 'feed' });
  const scroller = el('div', { id: 'messages', class: 'messages' }, feed);
  const notice = el('p', { class: 'chat-error error', id: 'chat-error', 'aria-live': 'polite' });
  // скринридеру — начало и конец ответа короткой фразой; сам текст ответа сюда не кладётся (поток не зачитывается кусками)
  const answering = el('p', { class: 'sr', id: 'chat-status', role: 'status' });
  const chipsBox = el('div', { class: 'att-chips' });
  const mediaBox = el('div', { class: 'att-chips chat-media', id: 'chat-media', 'aria-label': tr("Звук и видео диалога для MCP") });
  const input = el('textarea', { id: 'chat-input', rows: '1', placeholder: tr("Напишите сообщение…"), 'aria-label': tr("Сообщение"),
    maxlength: '200000' });
  const file = el('input', { type: 'file', id: 'attach', class: 'sr', accept: ACCEPT, multiple: true });
  const clip = el('label', { class: 'icon-btn attach', title: tr("Прикрепить файл") }, icon('clip'),
    el('span', { class: 'sr' }, tr("Прикрепить файл")), file);
  const send = el('button', { type: 'submit', id: 'send', class: 'btn btn-accent composer-go dialog-send' }, tr("Отправить"));
  const stop = el('button', { type: 'button', id: 'stop', class: 'send composer-go composer-stop', 'aria-label': tr("Остановить"),
    title: tr("Остановить"), hidden: true }, icon('stop'));
  // на узком экране слово кнопки прячется (.toggle-word), остаётся значок: слово — в aria-label и title
  const webToggle = el('button', { type: 'button', id: 'web-toggle', class: 'chip ctl-chip web-toggle', 'aria-pressed': 'false',
    'aria-label': tr("Интернет"), title: WEB_TITLE, hidden: true }, icon('globe'), el('span', { class: 'toggle-word' }, tr("Интернет")));
  const mcpToggle = el('button', { type: 'button', id: 'mcp-toggle', class: 'chip ctl-chip', hidden: true,
    'aria-label': tr("Инструменты MCP"), 'aria-haspopup': 'dialog' }, 'MCP');
  const thinkToggle = el('button', { type: 'button', id: 'think-toggle', class: 'chip ctl-chip think-toggle', 'aria-pressed': 'false',
    'aria-label': tr("Рассуждение"), title: THINK_TITLE, hidden: true }, icon('bulb'),
  el('span', { class: 'toggle-word' }, tr("Рассуждение")));
  // «Разговор» (chat-voice.js) — значок в поле ввода над микрофоном: нажат — круг идёт, его состояние — строкой под полем
  const talkToggle = iconButton('talk', tr("Разговор"), { id: 'talk-toggle', class: 'icon-btn talk', 'aria-pressed': 'false',
    title: TALK_TITLE, 'data-state': 'off' });
  const talkNote = el('p', { class: 'talk-note muted small', id: 'talk-note', 'aria-live': 'polite', hidden: true });
  let talking = null;          // stop() идущего «Разговора»
  let voiceWait = null;        // { text, update, resolve } — ход «Разговора», что ждёт своего ответа
  const webNote = el('p', { class: 'web-note muted small', id: 'web-note', 'aria-live': 'polite' });
  // роль диалога (roles.js): у открытого — `role` сервера, у нового — черновик до создания
  const roles = roleChooser({ current: () => (chat ? chat.chat.role : draftRole),
    choose: async (role) => {
      if (!chat) { draftRole = role; return; }
      const current = chat;
      const out = await api('PATCH', `/api/chats/${chat.chat.id}`, { role_id: role ? role.id : null });
      if (current === chat) chat.chat = out;
    },
    failed: (err) => { notice.textContent = words(err); } });
  // базы знаний диалога (knowledge.js): у открытого — `knowledge` сервера, у нового — черновик до создания
  const kbs = knowledgeChip({ current: () => (chat ? chat.knowledge || [] : draftKbs),
    choose: async (ids) => {
      if (!chat) { draftKbs = ids; return; }
      const current = chat;
      const out = await api('PUT', `/api/chats/${chat.chat.id}/knowledge`, { kb_ids: ids });
      if (current === chat) chat.knowledge = out.kb_ids;
    },
    failed: (err) => { notice.textContent = words(err); } });
  // уровень рассуждения (chat-effort.js): у открытого — `effort` сервера, у нового — черновик до создания
  const effort = effortChooser({ current: () => (chat ? chat.chat.effort : draftEffort),
    offered: () => (chat ? !!chat.chat.effort_ok
      : !!links.find((l) => l.connection.id === chosen().connection_id)?.connection.effort),
    choose: async (level) => {
      if (!chat) { draftEffort = level; return; }
      const current = chat;
      const out = await api('PATCH', `/api/chats/${chat.chat.id}`, { effort: level });
      if (current === chat) chat.chat = out;
    },
    failed: (err) => { notice.textContent = words(err); } });
  // «Разговор» уступает место «очистить», пока в поле текст (идущий — виден всегда): колонка — три значка, рамка
  // пустого поля не растёт с первой буквой
  const inputBox = fieldBox(input, { attach: clip, painted: () => { talkToggle.hidden = !talking && !!input.value; },
    dialog: voiceDialog, fold: true });
  const passportNote = el('p', { class: 'chat-passport', id: 'chat-passport', 'aria-live': 'polite', hidden: true });
  const attachmentNote = el('p', { class: 'chat-passport muted', id: 'chat-attachment-note', hidden: true }, MCP_TEXT_CHAT);
  const moveNote = el('p', { class: 'chat-passport', id: 'chat-move', 'aria-live': 'polite', hidden: true });
  const editNote = el('div', { class: 'chat-edit-note', hidden: true },
    el('span', {}, tr("Правка сообщения. После отправки последующие сообщения будут удалены.")),
    button(tr("Отменить правку"), { class: 'btn btn-sm chat-edit-cancel' }, () => {
      if (!sending) closeEdit();
    }));
  let passportKey = '';
  const ring = contextRing(estimateContext, { open: () => ctxWindow.open() });
  // навык над полем (chat-skills.js): меню «/» и чип #skill-chip; выбор и снятие — новая оценка контекста
  const skillHost = el('div', { class: 'chat-skill', id: 'chat-skill', hidden: true });
  const form = el('form', { class: 'composer', id: 'composer' }, mediaBox, chipsBox, passportNote, attachmentNote, moveNote, editNote, skillHost, inputBox.box, inputBox.error,
    el('div', { class: 'composer-bar' }, el('div', { class: 'composer-opts' }, modelWrap, routedTag, roles.chip, roles.menu, webToggle, mcpToggle,
      kbs.chip, kbs.panel, thinkToggle, effort.chip, effort.menu), send, stop),
    webNote, talkNote);
  const skills = chatSkills({ input, host: skillHost, changed: () => {
    ring.schedule();
    if (!editing && !queuing) dialogDrafts.typed(chatId);
  } });   // меню — перед полем формы
  const fieldTools = inputBox.box.querySelector('.field-tools');
  fieldTools.insertBefore(talkToggle, fieldTools.querySelector('.mic'));
  const projectAttach = iconButton('folder', tr("Прикрепить файл проекта"), { class: 'icon-btn', hidden: true });
  projectAttach.addEventListener('click', () => {
    const project = currentProject();
    const mine = opened;
    if (project) projectFilePicker(root, project, (file) => {
      if (alive && mine === opened && currentProject()?.id === project.id) addFiles([file]);
    });
  });
  fieldTools.prepend(projectAttach);
  let suggestKey = null;       // подсказки пустого экрана: { query, items } последнего запроса (items — когда пришли)
  const main = el('section', { class: 'chat-main' }, head, modelNote, scroller, notice, answering, form);
  const root = el('div', { class: 'chat' }, aside, backdrop, main);
  view.append(root);
  const findHost = el('div', { class: 'chat-find' });
  scroller.before(findHost);
  const find = findView(findHost, { scroller, feed, prefix: 'chat', texts: '.msg.user > .bubble, .msg.assistant .md' });
  const sharing = shareMenu(root, { id: 'chat-share', content: scroller, archive: true,
    failed: (err) => { notice.textContent = words(err); } });
  const share = sharing.button;
  share.hidden = find.button.hidden = true;
  // строка состояния над полем (chat-status.js), окна «Диалог и контекст», «Статистика», «Файлы диалога»
  const ctxWindow = contextWindow(root, { dialog: () => (chatId && chat?.chat.id === chatId ? chat : null),
    connection: () => connName.textContent, estimate: estimateContext,
    busy: summaryBusy, openWith });
  const status = chatStatus(root, { ring: ring.node, job: () => jobs.get(chatId) || null,
    last: () => {
      const m = chat?.chat.id === chatId ? chat.messages.findLast((x) => x.role === 'assistant') : null;
      return m ? liveOf(m) : null;
    },
    local: () => !!chat?.chat.local, stepNode, stats: () => statsDialog(root),
    files: showFiles, parts: partsButton({ root }),
    buttons: [share, title.actions, title.menu, find.button, title.pin] });
  form.before(status.bar);
  const inputDraft = sectionDraft('chat', input);      // поле общее для диалогов — и черновик один на раздел
  if (input.value) autosize();
  // черновик на каждый диалог (chat-drafts.js); sectionDraft остаётся только защитой от автоперезагрузки
  const dialogDrafts = chatDrafts(input, { autosize, skills, project: () => draftProject });
  // стрелки по своим сообщениям и «Развернуть все шаги» (message-nav.js) — справа у ленты
  const historyBox = el('div', { class: 'chat-history' });
  scroller.replaceWith(historyBox);
  const nav = messageNav({ scroller, feed, prefix: 'chat', own: '.msg.user', steps: 'details.web-steps, details.think',
    expandLabels: [tr("Развернуть все шаги"), tr("Свернуть все шаги")], stuck: () => stick,
    follow: (on) => { stick = on; found = null; } });
  historyBox.append(scroller, nav.node);
  const feedWatch = new MutationObserver(() => nav.paint());    // новый диалог, «Загружаю диалог…» — без стрелок
  feedWatch.observe(feed, { childList: true });
  feed.addEventListener('toggle', () => nav.paint(), true);       // шаг раскрыли рукой — подпись «Развернуть все»
  // очередь «После ответа» (chat-queue.js): уходит сама, когда ответ кончился и карточки нет
  const queueAdd = iconButton('wait', tr("После ответа"), { id: 'queue-add', class: 'send composer-go', hidden: true });
  stop.before(queueAdd);
  const queueHost = el('div', { hidden: true });
  form.before(queueHost);
  const queue = chatQueue(queueHost, { send: (item) => queuedTurn(item),
    failed: (err) => { notice.textContent = words(err); } });
  let queuing = null;          // { item, typed, back } — сообщение очереди уходит: его текст в поле на время submit
  let forkNote = null;         // { id, text } — слова для #chat-error ветки после её загрузки
  let idleTimer = 0;
  // вставка и перетаскивание файлов в поле — тем же путём, что скрепка
  fileDrop(form, (files) => addFiles(files));
  // «Новый диалог с итогом» у отказа по длине (chat-continue.js) — строкой под #chat-error
  const offer = continueOffer({ chatId: () => chatId, openWith, busy: summaryBusy,
    failed: (err) => { notice.textContent = words(err); } });
  notice.after(offer.node);
  // «Цитировать» и меню сообщения (quote.js, message-menu.js): цитата — чипом над полем, при отправке — в начало поля
  const quote = quoteChip(form, input, { id: 'chat-quote', changed: () => ring.schedule() });
  const quoteText = (node) => (node.matches('.msg.user') ? node.querySelector(':scope > .bubble')?.textContent || ''
    : chat?.messages.find((m) => m.id === node.dataset.id)?.content || '');
  const selQuote = selectionQuote(scroller, { texts: '.msg.user > .bubble, .msg.assistant .md', quote: quote.set });
  const msgMenu = messageMenu(scroller, { message: '.msg', text: quoteText, quote: quote.set });

  function shareContext() {
    const current = chat?.chat.id === chatId ? chat.chat : null;
    if (current?.protected) dropPreviews(root);     // сработал защищённый источник — окна HTML и SVG нет
    share.hidden = !current;
    find.button.hidden = !current;
    title.set(current, projectCaption());
    status.dialog(current ? chat : null);
    sharing.set(current ? { title: current.title || tr("Чат"), share: `/api/chats/${chatId}/share`,
      export: `/api/chats/${chatId}/export`, archive: `/api/chats/${chatId}/archive`,
      blocked: current.protected ? PROTECTED_SHARE : null } : null);
    listing.seen(current, !!current && chat.messages.some((m) => m.pending && m.status !== 'streaming'));
    tabs.show(current);
  }

  function showFiles() {
    if (!chatId || chat?.chat.id !== chatId) return;
    const shown = chat;
    filesDialog(root, chatId, (count) => {
      if (chat !== shown) return;
      chat.file_count = count;
      status.dialog(chat);
    });
  }

  /* выдвижная панель списка (≤ 760px) */
  function panel(open) {
    root.classList.toggle('panel-open', open);
    backdrop.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  }

  // список (chat-list.js): метки, звезда, поиск по названиям; вкладки диалогов над лентой (chat-tabs.js)
  const tabs = chatTabs({ open: (id) => (id ? ctx.navigate(`chat/${id}`) : newChat()), info: tabInfo,
    items: () => listed });
  const listing = chatList(list, { row: listItem, group: groupOf, pinItem, project: () => currentProject()?.id,
    failed: (err) => { notice.textContent = words(err); },
    loaded: (items) => { listed = items; tabs.sync(items.map((c) => c.id)); } });
  fresh.after(listing.search);
  modelNote.after(tabs.node);

  function currentProject() {
    return projects.find((item) => item.id === projectId) || projects.find((item) => item.inbox);
  }

  function projectCaption() {
    const project = currentProject();
    return project && !project.inbox ? projectName(project) : tr("Проекты");
  }

  function paintProjects() {
    projectsUI.paint(projects, currentProject());
    projectAttach.hidden = !currentProject();
    if (!chat && !chatId) title.set(null, projectCaption());
    listing.paint();
  }

  async function loadProjects() {
    try {
      const { items } = await api('GET', PROJECTS_API);
      if (!alive) return;
      projects = items;
      paintProjects();
    } catch (err) {
      if (alive) notice.textContent = words(err);
    }
  }

  async function moveProject(item) {
    await loadProjects();
    if (!alive || chatId !== item.id) return;
    chooseProject(root, tr("Переместить в проект"), projects.filter((p) => p.id !== item.project_id), async (pid) => {
      const out = await api('PATCH', `/api/chats/${item.id}`, { project_id: pid });
      if (alive && chat?.chat.id === out.id) {
        chat.chat = out;
        projectId = pid;
        paintProjects();
      }
      await loadList();
    });
  }

  function tabInfo(id) {
    const item = listed.find((c) => c.id === id);
    return { title: chat?.chat.id === id ? chat.chat.title : item?.title, busy: !!item?.busy || jobs.has(id) };
  }

  /* список диалогов */
  async function loadList() {
    await listing.reload();                          // удачный ответ — listed и tabs.sync через loaded
  }

  async function pinItem(item) {
    const out = await api('PATCH', `/api/chats/${item.id}`, { pinned: !item.pinned });
    if (!alive) return;
    if (item.id === chat?.chat.id) {
      chat.chat = { ...chat.chat, pinned: out.pinned };
      title.set(chat.chat);
    }
    await loadList();
  }

  function listItem(item) {
    const link = el('a', { href: `#/chat/${item.id}`, class: 'chat-link', 'aria-current': item.id === chatId ? 'page' : null },
      el('span', { class: 'chat-link-title' }, item.title || tr("Новый диалог")), chatTime(item.updated));
    link.addEventListener('click', () => {
      panel(false);
      // уже открытый диалог адрес не меняет — нажатие перечитывает его (ход могли сделать с другого устройства)
      if (item.id === chatId && !sending && !jobs.has(chatId)) reload();
    });
    return el('div', { class: 'chat-item', 'data-id': item.id }, link);
  }

  function renameDialog(item) {
    const title = el('input', { class: 'input', name: 'title', maxlength: '200', value: item.title || '' });
    const fail = el('p', { class: 'error' });
    const save = button(tr("Сохранить"), { class: 'btn btn-accent', id: 'rename-save' });
    const dialog = openDialog(root, { id: 'rename-dialog', title: tr("Переименовать диалог"),
      body: [field(tr("Название"), title, 'title'), fail], actions: [save] });
    const submit = async () => {
      save.disabled = true;
      try {
        const out = await api('PATCH', `/api/chats/${item.id}`, { title: title.value });
        if (chat && chat.chat.id === out.id) chat.chat = out;
        shareContext();
        dialog.close();
        loadList();
      } catch (err) {
        fail.textContent = words(err);
        save.disabled = false;
      }
    };
    save.addEventListener('click', submit);
    title.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        submit();
      }
    });
    title.focus();
    title.select();
  }

  function deleteDialog(item) {
    const fail = el('p', { class: 'error' });
    const confirm = button(tr("Удалить"), { class: 'btn btn-danger solid', id: 'chat-delete-confirm' });
    const dialog = openDialog(root, { id: 'chat-delete-dialog', title: tr("Удалить диалог?"),
      body: [el('p', {}, tr("«{0}» уйдёт в корзину на 14 дней вместе с вложениями — оттуда его можно вернуть.", item.title || tr("Новый диалог"))), fail], actions: [confirm] });
    confirm.addEventListener('click', async () => {
      confirm.disabled = true;
      try {
        await api('DELETE', `/api/chats/${item.id}`);
      } catch (err) {
        fail.textContent = words(err);
        confirm.disabled = false;
        return;
      }
      const job = jobs.get(item.id);
      if (job) job.controller.abort();
      dialog.close();
      if (chatId === item.id) newChat();
      loadList();
    });
  }

  /* открыть диалог с текстом в поле («Новый диалог с итогом», ветка): текст кладёт open() после загрузки */
  function openWith(id, text) {
    if (id === chatId && chat) {
      input.value = text;
      autosize();
      ring.schedule();
      if (!editing) dialogDrafts.typed(chatId);    // программная вставка — тоже черновик (возврат из bfcache)
      return;
    }
    prefill = { id, text };
    loadList();
    ctx.navigate('chat/' + id);
  }

  function newChat() {
    panel(false);
    const project = currentProject();
    const route = project && !project.inbox ? `chat?p=${project.id}` : 'chat';
    if (location.hash === `#/${route}`) open(null, false, project?.id);
    else ctx.navigate(route);
  }

  /* чип модели */
  function chosen() {
    if (chat) return { connection_id: chat.chat.connection_id, model: chat.chat.model, mcp_model_sid: chat.chat.mcp_model_sid };
    if (draft) return draft;
    return chatDefault ? { ...chatDefault } : { connection_id: null, model: '' };
  }

  function paintChip() {
    const { connection_id: connId, model, mcp_model_sid: sid } = chosen();
    file.disabled = !!sid;
    clip.setAttribute('aria-disabled', String(!!sid));
    clip.title = sid ? MCP_TEXT_CHAT : tr("Прикрепить файл");
    attachmentNote.hidden = !sid;
    if (!sid && notice.textContent === MCP_TEXT_CHAT) notice.textContent = '';
    const modelServer = mcpModelServers.find(server => server.id === sid);
    // ни подключения, ни модели (раздел без модели, подключение диалога удалено) — «по умолчанию» нечему
    const none = connId === null && !model;
    // подключение открытого диалога удалено (`connection_id` null, модель прежняя): ход ответит `no_connection`
    const deleted = !!chat && connId === null && !!model && !sid;
    const link = links.find((l) => l.connection.id === connId);
    // адрес подключения убран из списка администратора (`dropped`): ход ответит отказом
    const dropped = chat ? !!chat.chat.dropped : !!link?.connection.dropped;
    chipName.textContent = none ? tr("модель не выбрана") : deleted ? tr("подключение удалено — выберите модель")
      : model || tr("модель по умолчанию");
    chip.classList.toggle('off', none || deleted || dropped);
    const name = sid ? ['MCP', modelServer?.name].filter(Boolean).join(' · ') : link?.connection.name || '';
    connName.textContent = !dropped ? name : name ? tr("{0}: адрес убран из списка администратора", name)
      : tr("адрес подключения убран из списка администратора");
    chip.title = connName.textContent;
    droppedNote = dropped ? connName.textContent : '';     // в шапке подключения больше нет: отказ — строкой над лентой
    routedTag.hidden = !(chat ? chat.chat.routed : link?.connection.routed);
    if (moveFor !== chatId) {                       // предложение #chat-move — только под своим диалогом
      moveFor = null;
      moveNote.hidden = true;
      moveNote.replaceChildren();
    }
    paintGuard();
    paintThink();
    paintWeb();
    roles.paint();
    kbs.paint();
    paintSuggest();
    ring.schedule();
  }

  /* «Интернет» (как web.tools_for): 'hidden' — прямое подключение, кнопки нет; 'nokey' — через сервер без
   * ключа Exa, кнопка выключена с подсказкой; 'ready' — ход даст модели все инструменты; 'mcp' — ключа нет, но
   * набор MCP диалога даёт веб-инструменты (как mcp_client.web_tools): кнопка включается только ради них; 'cleaner' —
   * флажок администратора «Веб только через Cleaner», а диалог не через Cleaner: кнопка выключена с подсказкой.
   * Путь — `proxied` открытого диалога или подключения из чипа нового, ключ — профиль (перечитывается при возвращении) */
  function webState() {
    const mode = webBase();
    if (mode !== 'nokey') return mode;
    const ids = chat ? chat.chat.mcp_ids || [] : draftIds(draftMcp, mcpServers);
    return hasWebTools(catalog, ids, stateOf(chat ? chat.chat : draftMcp)) ? 'mcp' : 'nokey';
  }

  function webBase() {
    if (chat?.chat.cleaner || links.find((l) => l.connection.id === chosen().connection_id)?.connection.cleaner) return 'hidden';
    let proxied;
    if (chat) proxied = !!chat.chat.proxied;
    else {
      const link = links.find((l) => l.connection.id === chosen().connection_id);
      proxied = !!link && !!link.connection.proxied;
    }
    if (!proxied) return 'hidden';
    if (chat?.chat.protected) return 'protected';
    const routed = chat ? !!chat.chat.routed : !!links.find((l) => l.connection.id === chosen().connection_id)?.connection.routed;
    if (me?.cleaner_web && !routed) return 'cleaner';
    return me && me.exa_key ? 'ready' : 'nokey';
  }

  function webTools() {
    return webState() === 'ready' ? WEB_KINDS : [];
  }

  function webOn() {
    return ['ready', 'mcp'].includes(webState()) && (chat ? !!chat.chat.web : draftWeb);
  }

  function paintWeb() {
    const mode = webState();
    const hint = { nokey: WEB_HINT, protected: PROTECTED_HINT, cleaner: CLEANER_WEB_HINT }[mode] || '';
    webToggle.hidden = mode === 'hidden';
    webToggle.disabled = ['nokey', 'protected', 'cleaner'].includes(mode);
    webToggle.setAttribute('aria-pressed', String(webOn()));
    webToggle.title = hint || (mode === 'mcp' ? WEB_MCP_TITLE : WEB_TITLE);
    webNote.textContent = hint || (webOn() && thinkOn() ? THINK_NOTE : '');
    const conn = links.find(l => l.connection.id === chosen().connection_id)?.connection;
    mcpToggle.hidden = chat ? !chat.chat.mcp_ok : !conn?.proxied || !!conn?.cleaner;
    const count = mcpCount();
    mcpToggle.textContent = count ? `MCP (${count})` : 'MCP';
    mcpToggle.setAttribute('aria-pressed', String(count > 0));
    paintMcpState();
    paintMedia();
  }

  /* точка состояния у чипа MCP: худшее состояние действующих серверов со `studio/status` (GET /api/mcp/status) */
  function paintMcpState() {
    const ids = chat ? chat.chat.mcp_ids || [] : draftIds(draftMcp, mcpServers);
    const known = ids.filter((id) => mcpStatus[id])
      .map((id) => ({ ...mcpStatus[id], name: mcpServers.find((s) => s.id === id)?.name || 'MCP' }));
    const worst = ['down', 'busy', 'ok'].find((state) => known.some((item) => item.state === state));
    if (worst) mcpToggle.dataset.mcpState = worst;
    else delete mcpToggle.dataset.mcpState;
    mcpToggle.title = known.map((item) => `${item.name}: ${mcpStateWords(item)}`).join(' · ');
  }

  /* звук и видео диалога (как mcp_files.media_limit): через сервер, не Cleaner; серверы набора с `chat_files`, кроме
     «Не передавать». null — не принимаются; `ask` — у кого спросить согласие (ни один ещё не разрешён), `limit` — наибольший
     предел разрешённых (или тех, у кого спросим) */
  function mediaState() {
    let ids;
    let decisions = {};
    if (chat) {
      if (!chat.chat.proxied || chat.chat.cleaner) return null;
      ids = chat.chat.mcp_ids || [];
      decisions = chat.chat.mcp_file_consent || {};
    } else {
      const conn = links.find((l) => l.connection.id === chosen().connection_id)?.connection;
      if (!conn?.proxied || conn.cleaner) return null;
      ids = draftIds(draftMcp, mcpServers);
    }
    // флажок «Веб только через Cleaner»: файлы идут на сервер мимо Cleaner — веб-загрузка закрыта и через Cleaner
    const targets = mediaTargets(catalog, ids, stateOf(chat?.chat ?? draftMcp),
      !me?.cleaner_web && (chat ? !!chat.chat.web : webOn()))
      .filter((t) => decisions[String(t.id)] !== 'refused');
    if (!targets.length) return null;
    const allowed = targets.filter((t) => decisions[String(t.id)] === 'allowed');
    return { ask: allowed.length ? [] : targets, limit: Math.max(...(allowed.length ? allowed : targets).map((t) => t.max)) };
  }

  function paintMedia() {
    file.accept = mediaState() ? `${ACCEPT},${MEDIA_ACCEPT}` : ACCEPT;
    mediaBox.replaceChildren(...(chat?.media || []).map((item) => {
      const chip = mediaChip(item);
      const remove = iconButton('x', tr("Удалить {0}", item.name), { class: 'icon-btn att-remove' });
      remove.addEventListener('click', () => deleteMedia(item));
      chip.append(remove);
      return chip;
    }), ...pendingMedia.map((item) => {
      const chip = mediaChip(item);
      if (!item.busy) {
        const remove = iconButton('x', tr("Убрать {0}", item.name), { class: 'icon-btn att-remove' });
        remove.addEventListener('click', () => {
          pendingMedia = pendingMedia.filter((x) => x !== item);
          paintMedia();
        });
        chip.append(remove);
      }
      return chip;
    }));
    paintPassport();                                 // каталог MCP и согласие — для строки паспорта
  }

  function deleteMedia(item) {
    const id = chatId;
    const fail = el('p', { class: 'error small', role: 'alert' });
    const confirm = button(tr("Удалить"), { class: 'btn btn-danger solid', id: 'chat-media-delete-confirm' });
    const dialog = openDialog(root, { id: 'chat-media-delete-dialog', title: tr("Удалить файл?"),
      body: [el('p', {}, item.name), el('p', {}, tr("Файл уйдёт в корзину на 14 дней — оттуда его можно вернуть.")), fail],
      actions: [confirm] });
    confirm.addEventListener('click', async () => {
      confirm.disabled = true;
      fail.textContent = '';
      try {
        await api('DELETE', `/api/chats/${id}/media/${item.id}`);
        if (chatId === id && chat && chat.media?.some((entry) => entry.id === item.id)) {
          chat.media = chat.media.filter((entry) => entry.id !== item.id);
          chat.file_count = Math.max(0, chat.file_count - 1);
          status.dialog(chat);
          paintMedia();
        }
        dialog.close();
      } catch (err) {
        fail.textContent = words(err);
        confirm.disabled = false;
      }
    });
  }

  /* согласие на звук нового диалога или диалога, где серверу ещё не разрешали файлы: окно со списком файлов диалога */
  function askMediaConsent(targets, files) {
    const names = [...new Set([...(chat ? chat.messages : []).flatMap((m) => (m.attachments || []).map((a) => a.name)),
      ...(chat?.media || []).map((m) => m.name), ...files.map((f) => f.name)])];
    const allow = button(tr("Передать"), { class: 'btn btn-accent', id: 'media-consent-allow' });
    const dialog = openDialog(root, { id: 'media-consent-dialog', title: tr("Файлы диалога для MCP"), cancelText: tr("Не передавать"),
      actions: [allow], body: [el('p', {}, tr("Передать серверу «{0}» файлы диалога: {1}?", targets.map((t) => t.name).join(', '),
        names.join(', ')))] });
    return new Promise((resolve) => {
      let yes = false;
      allow.addEventListener('click', () => { yes = true; dialog.close(); });
      dialog.addEventListener('close', () => resolve(yes), { once: true });
    });
  }

  /* звук и видео из поля ввода: больше предела — отказ до загрузки; без согласия — окно; новый диалог — создать */
  async function pickMedia(files, state) {
    const fit = [];
    for (const f of files) {
      if (f.size > state.limit) {
        pendingMedia.push({ name: f.name, mime: f.type, size: f.size,
          error: tr("больше {0} — предела MCP этого диалога", byteSize(state.limit)) });
      } else fit.push(f);
    }
    paintMedia();
    if (!fit.length || (state.ask.length && !(await askMediaConsent(state.ask, fit)))) return;
    const made = {};
    let saved = 0;
    try {
      await ensureChat(made);
      const id = chatId;
      for (const t of state.ask) {
        const out = await api('POST', `/api/chats/${id}/mcp-consent`, { server_id: t.id, allow: true });
        if (chatId === id) chat.chat.mcp_file_consent = out.mcp_file_consent;
      }
      paintConsents();
      for (const f of fit) {
        if (chatId !== id) break;                  // ушли в другой диалог — остальное не грузить
        const item = { name: f.name, mime: f.type, size: f.size, busy: true };
        pendingMedia.push(item);
        paintMedia();
        try {
          const done = await uploadMedia(id, f);
          pendingMedia = pendingMedia.filter((x) => x !== item);
          saved += 1;
          if (chatId === id && !(chat.media || []).some((entry) => entry.id === done.id)) {
            chat.media = [...(chat.media || []), done];
            chat.file_count += 1;
            status.dialog(chat);
          }
        } catch (err) {
          item.busy = false;
          item.error = words(err);
        }
        paintMedia();
      }
    } catch (err) {
      notice.textContent = words(err);
    }
    if (made.id && !saved) dropEmpty(made.id);
  }

  /* «Передать» / «Не передавать» файлы диалога серверу MCP: решение — во все блоки согласия ленты и в поле ввода */
  async function decideConsent(sid, allow) {
    const id = chatId;
    try {
      const out = await api('POST', `/api/chats/${id}/mcp-consent`, { server_id: sid, allow });
      if (chatId !== id || !chat) return;
      chat.chat.mcp_file_consent = out.mcp_file_consent;
      notice.textContent = '';
    } catch (err) {
      notice.textContent = words(err);
      return;
    }
    paintConsents();
    paintMedia();
  }

  function paintConsent(m) {
    const p = parts.get(m);
    if (!p) return;
    const asks = consentAsks(answerSteps(m));
    const decisions = chat?.chat.mcp_file_consent || {};
    const key = JSON.stringify([asks, asks.map((a) => decisions[String(a.sid)] || '')]);
    if (p.consentKey === key) return;
    p.consentKey = key;
    p.consent.replaceChildren(...asks.map((a) => consentNode(a, decisions[String(a.sid)], decideConsent)));
  }

  function paintConsents() {
    for (const m of chat ? chat.messages : []) paintConsent(liveOf(m));
  }

  /* действующие серверы MCP: у открытого диалога — `mcp_ids` сервера, у нового — черновик поверх общих по умолчанию
     (прямое подключение и Cleaner общих по умолчанию не получают, как mcp_sources.dialog_servers) */
  function mcpCount() {
    if (chat) return (chat.chat.mcp_ids || []).length;
    const conn = links.find(l => l.connection.id === chosen().connection_id)?.connection;
    if (!conn?.proxied || conn?.cleaner) return draftMcp ? draftMcp.mcp_ids.length : 0;
    return draftCount(draftMcp, mcpServers);
  }

  mcpToggle.addEventListener('click', () => {
    const current = chat, mine = opened;
    chooseDialogMcp(root, stateOf(chat ? chat.chat : draftMcp), async (body) => {
      if (mine !== opened || current !== chat) return;
      if (chat) chat.chat = await api('PATCH', `/api/chats/${chat.chat.id}`, body);
      else draftMcp = body;
      paintWeb();
      const host = feed.querySelector('.chat-suggest');   // подсказки MCP — уже другого набора
      if (host && chat) { suggestKey = null; delete host.dataset.query; paintSuggest(host); }
    });
  });

  /* 409 `no_exa_key`: ключ убрали в другой вкладке — профиль этой вкладки ещё не знал */
  function noKey(err) {
    if (err.code !== 'no_exa_key') return false;
    if (me) me.exa_key = false;
    notice.textContent = words(err);
    paintWeb();
    return true;
  }

  async function switchWeb() {
    if (!chat) {
      draftWeb = !draftWeb;
      paintWeb();
      return;
    }
    webToggle.disabled = true;
    try {
      chat.chat = await api('PATCH', `/api/chats/${chat.chat.id}`, { web: !webOn() });
      notice.textContent = '';
    } catch (err) {
      if (!noKey(err)) notice.textContent = words(err);
    } finally {
      webToggle.disabled = false;
    }
    paintWeb();
  }

  /* «Рассуждение» (как chat._thinking_ok сервера): у открытого диалога — `thinking_ok`, у нового — модель чипа
   * локальная (роль `local-*` srvllm через сервер, `local_models` подключения) */
  function thinkOk() {
    return chat ? !!chat.chat.thinking_ok : isLocal(chosen());
  }

  function thinkOn() {
    return thinkOk() && (chat ? !!chat.chat.thinking : draftThink);
  }

  function paintThink() {
    thinkToggle.hidden = !thinkOk();
    thinkToggle.setAttribute('aria-pressed', String(thinkOn()));
    effort.paint();
  }

  /* «Разговор»: нажатие — круг chat-voice.js (звук разблокируется этим нажатием), ещё нажатие — стоп */
  function switchTalk() {
    if (talking) {
      talking();
      return;
    }
    notice.textContent = '';
    try {
      talking = startConversation({ send: voiceTurn, state: paintTalk, dialog: voiceDialog,
        failed: (text) => { notice.textContent = text; } });
    } catch (err) {                                  // браузер без Web Audio
      notice.textContent = words(err);
    }
  }

  function voiceDialog() {
    return chatId ? { chat_id: chatId } : { connection_id: chosen().connection_id };
  }

  function paintTalk(state) {
    if (state === 'off') talking = null;
    talkToggle.hidden = state === 'off' && !!input.value;
    talkToggle.dataset.state = state;
    talkToggle.setAttribute('aria-pressed', String(state !== 'off'));
    talkNote.hidden = state === 'off';
    talkNote.textContent = TALK_WORDS[state] || '';
  }

  /* ход «Разговора» — обычный ход с текстом фразы; набранное в поле до него возвращается в поле. Итог — статус
     ответа или false: хода не было (идёт другой ответ, отказ — словами в #chat-error) */
  function voiceTurn(text, update) {
    if (editing || sending || jobs.has(chatId)) return Promise.resolve(false);
    const typed = input.value;
    input.value = text;
    return new Promise((resolve) => {
      const wait = { text, update, resolve };
      voiceWait = wait;
      submit(false).finally(() => {
        if (voiceWait === wait) {
          voiceWait = null;
          resolve(false);
        }
        if (!input.value && typed) input.value = typed;
        autosize();
      });
    });
  }

  /* «После ответа» (chat-queue.js): текст поля — в очередь открытого диалога; правка сообщения, вложения и навык
     (чип chat-skills.js) в очередь не встают. Принято — поле и черновик диалога пусты */
  function enqueue() {
    const text = quote.composed();
    if (!chatId) return;
    if (editing) {                    // ответ пошёл (с другого устройства), пока открыта правка: она ждёт конца ответа
      notice.textContent = tr("Ответ ещё идёт — сохраните правку после него");
      return;
    }
    if (!text.trim() && !atts.length && !skills.chosen()) return;
    if (atts.length) {
      notice.textContent = tr("В очередь встаёт только текст — вложения отправьте после ответа");
      return;
    }
    if (skills.chosen()) {
      notice.textContent = tr("В очередь встаёт только текст — навык выберите после ответа");
      return;
    }
    // текст черновика с квитанцией (ушло ли — неизвестно) встаёт в очередь с тем же `request_id`
    if (!queue.add(chatId, text, dialogDrafts.receipt(chatId, text))) {
      notice.textContent = tr("В очереди не больше {0} сообщений", QUEUE_MAX);
      return;
    }
    notice.textContent = '';
    quote.clear();
    inputDraft.sent(text);
    input.value = '';
    autosize();
    dialogDrafts.typed(chatId);
    ring.schedule();
  }

  /* сообщение очереди уходит обычным ходом (как voiceTurn): его текст — в поле на время submit (поле только для
     чтения), набранное и вложения поля ждут и возвращаются. Итог — `done`, `unknown` (исход неизвестен: ждём
     квитанцию), `busy` (ход на другом устройстве — подождать) или `failed` (очередь — на паузу) */
  async function queuedTurn(item) {
    const held = atts;
    atts = [];
    // окно подтверждения потом отправило бы текст поля, а не сообщение очереди: такой ход — только рукой
    if (needsConsent(false) || needsWebConsent(false)) {
      atts = held;
      notice.textContent = tr("Сообщение из очереди требует подтверждения — отправьте его сами");
      return 'failed';
    }
    const heldSkill = skills.chosen();               // чип навыка — у набранного в поле, не у сообщения очереди
    if (heldSkill) skills.clear();
    queuing = { item, typed: input.value, back: false };
    input.value = item.text;
    input.readOnly = true;
    let refusal = null;
    try {
      refusal = await submit(false);
    } finally {
      atts = [...held, ...atts];
      unqueueField();
      queuing = null;
      if (heldSkill && !skills.chosen()) skills.restore(heldSkill);
      paintAtts();
    }
    if (pendingTurn?.queued === item.id && pendingTurn.uncertain) return 'unknown';
    return refusal?.code === 'busy' ? 'busy' : 'failed';      // принятое сообщение очередь уже убрала (done)
  }

  /* поле — снова набранное человеком (уход сообщения очереди кончился, смена диалога, уход из раздела) */
  function unqueueField() {
    if (!queuing || queuing.back) return;
    queuing.back = true;
    input.readOnly = false;
    input.value = queuing.typed;
    autosize();
  }

  /* файлы из скрепки, буфера и перетаскивания (chat-drop.js): звук и видео — в диалог для MCP, если он их примет;
     не подходящие — словами у чипа сразу */
  function addFiles(picked) {
    if (chosen().mcp_model_sid) {
      notice.textContent = MCP_TEXT_CHAT;
      return;
    }
    const media = mediaState();
    if (media && picked.some(isMedia)) pickMedia(picked.filter(isMedia), media);
    for (const f of media ? picked.filter((x) => !isMedia(x)) : picked) {
      const error = unfit(f);
      atts.push({ file: f, meta: null, error: error && words(error), busy: false, unfit: !!error });   // как отказ сервера
    }
    dropWaiting();
    if (atts.length > ATTACH_MAX) notice.textContent = tr("не больше {0} вложений в сообщении", ATTACH_MAX);
    paintAtts();
  }

  async function switchThink() {
    if (!chat) {
      draftThink = !draftThink;
    } else {
      thinkToggle.disabled = true;
      try {
        chat.chat = await api('PATCH', `/api/chats/${chat.chat.id}`, { thinking: !thinkOn() });
        notice.textContent = '';
      } catch (err) {
        if (err.code === 'no_thinking') chat.chat.thinking_ok = false;   // модель сменили в другой вкладке
        notice.textContent = words(err);
      } finally {
        thinkToggle.disabled = false;
      }
    }
    paintThink();
    paintWeb();
  }

  /* У локальной модели включается кнопка «Рассуждение». */
  function isLocal({ connection_id: connId, model }) {
    const link = links.find((l) => l.connection.id === connId);
    return !!link && (link.local_models || []).includes(model);
  }

  /* ход унесёт расшифровку во внешний сервис без подтверждения: в этом ходе (новое вложение) или в
   * истории открытого диалога (`external` сервера — после смены модели, и для «Ответить заново») */
  function needsConsent(regenerate) {
    const fresh = !regenerate && atts.some((a) => a.transcript);
    if (chat) return !!chat.chat.external || (fresh && !chat.chat.transcript_consent_free);
    if (!fresh) return false;
    const { connection_id: id, model } = chosen();
    const link = links.find((l) => l.connection.id === id);
    return !(link?.connection.cleaner || link?.connection.routed || link?.transcript_local_models?.includes(model));
  }

  /* ход даст модели Exa, а запросы она составит из расшифровки без подтверждения: в истории (`external_web`
   * сервера; он не считает неотправленное вложение) или в этом ходе (новое вложение из расшифровки) */
  function needsWebConsent(regenerate) {
    if (!mcpCount() && (!webOn() || !webTools().includes('search'))) return false;
    const fresh = !regenerate && atts.some((a) => a.transcript);
    return fresh || (!!chat && !!chat.chat.external_web);
  }

  function transcriptPair() {
    const preferred = links.find((link) => link.connection.id === chatDefault?.connection_id);
    if (preferred?.transcript_local_models?.includes(chatDefault.model)) return { ...chatDefault };
    for (const link of links) {
      const model = link.transcript_local_models?.[0];
      if (model) return { connection_id: link.connection.id, model };
    }
    return null;
  }

  function blocked() {
    return needModel && !chatId && atts.some((a) => a.transcript);
  }

  function paintGuard() {
    modelNote.textContent = blocked() ? EXTERNAL_NOTE : droppedNote;
    send.disabled = sending || blocked();
    paintPassport();
  }

  /* фото поля ввода, которые уйдут с ходом (не отвергнутые при выборе или сервером) */
  function photoAtts() {
    return atts.filter((a) => !a.error && (a.file ? a.file.type.startsWith('image/') : a.meta.kind === 'image'));
  }

  /* строка паспорта (п. 31): в возможностях модели чипа есть запись без `vision`; нет
     паспорта или записи — молчим. Сервер MCP набора с `chat_files` и согласием «Передать» — называем его */
  function paintPassport() {
    const { connection_id: connId, model } = chosen();
    const caps = links.find((l) => l.connection.id === connId)?.capabilities;
    const blind = photoAtts().length > 0 && !!caps && Object.hasOwn(caps, model) && !caps[model].includes('vision');
    const decisions = chat?.chat.mcp_file_consent || {};
    const tool = blind && chat ? mediaTargets(catalog, chat.chat.mcp_ids || [], stateOf(chat.chat),
      !!chat.chat.web && !me?.cleaner_web)
      .find((t) => decisions[String(t.id)] === 'allowed') : null;
    const key = blind ? `blind:${tool ? tool.name : ''}` : '';
    if (key === passportKey) return;                 // ссылки не пересоздаются: фокус на них не теряется
    passportKey = key;
    passportNote.hidden = !blind;
    passportNote.replaceChildren(...(!blind ? [] : [el('span', {}, tr("Модель по паспорту не смотрит фото")),
      tool ? el('span', { class: 'chat-passport-mcp' }, tr("Фото получит инструмент MCP «{0}»", tool.name)) : null,
      button(tr("Убрать фото"), { class: 'btn-link passport-drop' }, () => {
        const gone = photoAtts();
        atts = atts.filter((a) => !gone.includes(a));
        for (const a of gone) if (a.meta) api('DELETE', `/api/attachments/${a.meta.id}`).catch(() => {});
        dropWaiting();
        paintAtts();
        input.focus({ preventScroll: true });
      }),
      button(tr("Сменить модель"), { class: 'btn-link passport-model' }, () => chip.click())].filter(Boolean)));
  }

  /* оценка контекста открытого диалога (chat-context.js): загруженные вложения — сервером, ещё не загруженные
     (грузятся при отправке) — здесь грубо: фото — 1000 токенов (как chat.estimate_tokens), текстовый файл — байты / 3,
     PDF и DOCX — не оценить до разбора. Новый диалог — нечего оценивать */
  async function estimateContext() {
    const id = chatId;
    if (!id || chat?.chat.id !== id || editing) return null;
    const sent = atts.filter((a) => a.meta && !a.error);
    const local = atts.filter((a) => !a.meta && !a.error && a.file);
    const skillId = skills.chosen()?.id;
    const out = await api('POST', `/api/chats/${id}/estimate`, { text: quote.composed(),
      attachment_ids: sent.slice(0, ATTACH_MAX).map((a) => a.meta.id), ...(skillId ? { skill_id: skillId } : {}) });
    if (chatId !== id) return null;
    const guess = local.reduce((sum, a) => sum + (a.file.type.startsWith('image/') ? 1000
      : /\.(pdf|docx)$/i.test(a.file.name) ? 0 : Math.ceil(a.file.size / 3)), 0);
    return { tokens: out.tokens + guess, limit: out.limit };
  }

  /* подсказки пустого экрана (chat-suggest.js): открытый пустой диалог — его подключение и MCP, новый — пара чипа и
     MCP по умолчанию; пара та же — не перезапрашиваются */
  function paintSuggest(host = feed.querySelector('.chat-suggest')) {
    if (!host) return;
    const { connection_id: connId, model } = chosen();
    const query = chatId ? `chat_id=${encodeURIComponent(chatId)}`
      : connId === null ? '' : `connection_id=${connId}&model=${encodeURIComponent(model || '')}`;
    if (host.dataset.query === query) return;
    host.dataset.query = query;
    if (suggestKey?.query === query && suggestKey.items) { paintSuggestions(host, suggestKey.items, input); return; }
    paintSuggestions(host, [], input);
    if (!query) { suggestKey = null; return; }
    const mine = suggestKey = { query, items: null };
    api('GET', `/api/chats/suggestions?${query}`).then((items) => {
      mine.items = items;
      if (mine === suggestKey && host.isConnected) paintSuggestions(host, items, input);
    }, () => {});
  }

  /* окна подтверждений — по очереди: сначала внешняя модель, затем Exa; каждое несёт уже данное другое */
  function askExternal(regenerate, webOk) {
    const link = links.find((l) => l.connection.id === chosen().connection_id);
    const confirm = button(tr("Отправить"), { class: 'btn btn-accent', id: 'external-confirm' });
    const dialog = openDialog(root, { id: 'external-dialog', title: tr("Отправить расшифровку?"), actions: [confirm],
      body: [el('p', {}, tr("Расшифровка уйдёт во внешний сервис «{0}» — отправить?", link ? link.connection.name : tr("подключение диалога")))] });
    confirm.addEventListener('click', () => {
      dialog.close();
      submit(regenerate, true, webOk);
    });
  }

  function askWeb(regenerate, externalOk) {
    const confirm = button(tr("Отправить"), { class: 'btn btn-accent', id: 'web-confirm' });
    const dialog = openDialog(root, { id: 'web-dialog', title: tr("Расшифровка и интернет"), actions: [confirm],
      body: [el('p', {}, mcpCount()
        ? tr("Запросы из расшифровки могут уйти на выбранные MCP-серверы — отправить?") : WEB_ASK)] });
    confirm.addEventListener('click', () => {
      dialog.close();
      submit(regenerate, externalOk, true);
    });
  }

  /** Готовый вопрос из расшифровки или скана — в поле: набранный и не отправленный текст (черновик раздела) не
   * стирается, вопрос встаёт после него через пустую строку; «Обсудить» (вопроса нет) поле не трогает. */
  function putAsk(question) {
    if (!input.value.trim()) input.value = question;
    else if (question) input.value = `${input.value.trimEnd()}\n\n${question}`;
    if (!editing) dialogDrafts.typed(chatId);      // программная вставка — тоже черновик (возврат из bfcache)
  }

  async function fromTranscript(tid, ask) {
    history.replaceState(history.state, '', '#/chat');      // перезагрузка не сделает второе вложение
    open(null);
    if (ctx.hint) return;                           // без модели чата новый диалог не начать
    notice.textContent = '';
    putAsk(ASKS[ask] || '');
    autosize();
    await sectionsReady;
    draft = transcriptPair();
    needModel = !draft;
    draft ||= { connection_id: null, model: '' };
    paintChip();
    let meta;
    try {
      meta = await api('POST', `/api/transcripts/${encodeURIComponent(tid)}/attachments`);
    } catch (err) {
      notice.textContent = words(err);
      draft = null;                                 // вложения нет — обычный новый диалог: чип — как его создаст сервер
      needModel = false;
      paintChip();
      return;
    }
    atts.push({ file: null, meta, error: '', busy: false, transcript: tid });
    paintAtts();
  }

  /* из «Распознавания» (#/chat?ocr=<id>&ask=…): как из расшифровки, но модель — умолчание раздела и без окна
     внешней модели (спека OCR 6.4: документ свой) */
  async function fromScan(sid, ask) {
    history.replaceState(history.state, '', '#/chat');      // перезагрузка не сделает второе вложение
    open(null);
    if (ctx.hint) return;
    notice.textContent = '';
    putAsk(SCAN_ASKS[ask] || '');
    autosize();
    let meta;
    try {
      meta = await api('POST', `/api/ocr/${encodeURIComponent(sid)}/attachments`);
    } catch (err) {
      notice.textContent = words(err);
      return;
    }
    atts.push({ file: null, meta, error: '', busy: false, scan: sid });
    paintAtts();
  }

  function showText(a) {
    const tokens = (a.meta.tokens / 1000).toLocaleString(locale(), { maximumFractionDigits: 1 });
    openDialog(root, { id: 'att-text-dialog', title: a.meta.name, cancelText: tr("Закрыть"),
      body: [el('p', { class: 'muted small' }, tr("{0} зн. · ~{1} тыс. токенов", a.meta.chars.toLocaleString(locale()), tokens)),
        el('pre', { class: 'att-text' }, a.meta.text)] });
  }

  /** Пункты меню моделей при открытии: отмеченные модели раздела «Чат» по подключениям. */
  function fillModels(close) {
    const now = chosen();
    const shown = links.filter((l) => l.models.length);
    if (!shown.length && !mcpModelServers.length) {
      return [[el('p', { class: 'muted small menu-note' }, tr("Нет отмеченных моделей — выберите в настройках раздела «Чат»"))], []];
    }
    const items = [];
    const nodes = shown.map(({ connection: conn, models: ids }) => {
      const name = shown.length > 1 ? el('p', { class: 'model-group-name', id: `model-group-${conn.id}` }, conn.name) : null;
      const group = el('div', { class: 'model-group', role: 'group', 'data-conn': String(conn.id),
        'aria-labelledby': name ? name.id : null }, name);
      for (const id of ids) {
        const option = menuItem(id, { radio: true, checked: conn.id === now.connection_id && id === now.model,
          attrs: { class: 'model-option', 'data-conn': String(conn.id), 'data-model': id },
          onSelect: () => { close(true); choose(conn.id, id); } });
        items.push(option);
        group.append(option);
      }
      return group;
    });
    if (!chat?.chat.routed && !chat?.chat.cleaner) {
      for (const server of mcpModelServers) {
        const name = el('p', { class: 'model-group-name', id: 'mcp-model-group-' + server.id }, 'MCP · ' + server.name);
        const group = el('div', { class: 'model-group', role: 'group', 'aria-labelledby': name.id }, name);
        for (const id of server.models) {
          const option = menuItem(id, { radio: true, checked: server.id === now.mcp_model_sid && id === now.model,
            attrs: { class: 'model-option', 'data-mcp': String(server.id), 'data-model': id },
            onSelect: () => { close(true); choose(null, id, server.id); } });
          items.push(option);
          group.append(option);
        }
        nodes.push(group);
      }
    }
    return [nodes, items];
  }

  async function choose(connId, model, sid) {
    models.close();
    needModel = false;                             // явный выбор
    if (!chat) {
      draft = sid ? { mcp_model_sid: sid, model } : { connection_id: connId, model };
      paintChip();
      return;
    }
    const was = chat.chat;
    try {
      const updated = await api('PATCH', `/api/chats/${was.id}`, sid ? { mcp_model_sid: sid, model } : { connection_id: connId, model });
      if (chatId !== was.id) return;
      chat.chat = updated;
      notice.textContent = '';
    } catch (err) {
      if (chatId !== was.id) return;
      notice.textContent = words(err);
    }
    // подключение было удалено, закрыто группами доступа (`closed_connection_id`) или убрано из списка
    // администратора — те же диалоги списка можно перевести разом
    if (!sid && !was.mcp_model_sid && chat.chat !== was && ((was.connection_id === null && was.model) || was.dropped)) {
      offerMove(was.connection_id ?? was.closed_connection_id ?? null, connId, model);
    }
    paintChip();
  }

  function offerMove(from, connId, model) {
    const others = listed.filter((c) => c.id !== chatId && !c.mcp_model_sid && c.connection_id === from).length;
    if (!others) return;
    moveFor = chatId;
    const go = button(tr("Перевести их"), { class: 'btn-link', id: 'chat-move-go' }, async () => {
      go.disabled = true;
      try {
        const out = await api('POST', '/api/chats/move', { from_connection_id: from, connection_id: connId, model });
        moveNote.replaceChildren(el('span', {}, out.kept
          ? tr("Переведено диалогов: {0}. Остались прежними — с MCP: {1}", out.moved, out.kept)
          : tr("Переведено диалогов: {0}", out.moved)));
        loadList();
      } catch (err) {
        moveNote.replaceChildren(el('span', { class: 'error' }, words(err)));
      }
    });
    moveNote.replaceChildren(el('span', {}, tr("Ещё диалогов на прежнем подключении: {0} — перевести их на «{1}»?", others, model)), go);
    moveNote.hidden = false;
  }

  /* лента */
  function liveOf(m) {
    const job = jobs.get(chatId);
    if (job && m.id && job.message.id === m.id) return job.message;
    // несохранённый ответ этой вкладки: сервер держит его `streaming` («прервано») — показать текст из памяти
    const kept = m.id ? unsaved.get(m.id) : null;
    // закрытый сервером без текста (брошен по сроку) — текст из памяти, чтобы его скопировать
    if (kept && (m === kept.message || m.status === 'streaming' || (kept.closed && !m.content))) return kept.message;
    if (kept) unsaved.delete(m.id);                  // сохранила другая вкладка или ответ заменён
    return m;
  }

  function reading(m) {
    for (const job of jobs.values()) if (job.message === m) return job;
    return null;
  }

  /* отправленное фото целиком — в окне студии (её сессией: PWA на iPhone не делит cookie с Safari), «Скачать» — saveMedia */
  function showPhoto(a) {
    const save = button(tr("Скачать"), { class: 'btn btn-accent', id: 'att-photo-save' }, () => saveMedia(a.media_id, root));
    openDialog(root, { id: 'att-photo-dialog', title: a.name, cancelText: tr("Закрыть"), actions: [save],
      body: [el('img', { class: 'att-photo', src: `/api/media/${encodeURIComponent(a.media_id)}`, alt: a.name })] });
  }

  /* вложение отправленного сообщения: фото — миниатюра, нажатие — окно с ним; документ — имя ссылкой на скачивание */
  function attLabel(a) {
    const file = `/api/media/${encodeURIComponent(a.media_id)}`;
    let name = el('b', {}, a.name);
    let mark = icon(a.kind === 'image' ? 'photo' : 'doc');
    if (a.media_id && a.kind === 'image') {
      mark = imageThumb(a, showPhoto);
    } else if (a.media_id) {
      name = el('a', { class: 'att-file', href: `${file}?download=1`, download: '', title: tr("Скачать") }, name);
    }
    return el('span', { class: 'att' }, mark, name,
      // идущий скан (`ocr` ответа загрузки или вложения диалога) знаков ещё не знает — их скажет перечитанный диалог
      a.kind === 'document' && !a.ocr ? el('span', { class: 'muted' }, tr("{0} зн.", Number(a.chars || 0).toLocaleString(locale()))) : null,
      // последний ход отдал документ модели выдержками: он не поместился в её контекст (или предел не известен)
      a.excerpts ? el('span', { class: 'muted att-excerpts' }, tr("модель видит выдержки")) : null,
      a.source && a.source.exists ? el('a', { class: 'att-source',
        href: `#/transcribe?id=${encodeURIComponent(a.source.transcript_id)}` }, tr("к записи")) : null,
      // скан вложения кончился с нераспознанными страницами: тот же документ — снова в очередь, текст — его итог
      a.rescan ? button(tr("Распознать заново"), { class: 'btn btn-sm att-rescan' }, async (event) => {
        event.currentTarget.disabled = true;
        try {
          await api('POST', `/api/attachments/${encodeURIComponent(a.id)}/ocr`);
          kick();
          notice.textContent = '';
        } catch (err) {
          notice.textContent = words(err);
        }
        reload();
      }) : null);
  }

  /* «Очищенная копия» диалога с Cleaner: текст сообщения с его документами (у ответа — восстановленный текст) после
     замен Cleaner — без обращения к модели */
  function cleanerCopy(m) {
    return button(tr("Очищенная копия"), { class: 'btn btn-sm msg-cleaner-copy' }, async () => {
      try {
        const result = await api('POST', `/api/chats/${chatId}/cleaner-copy`, { message_id: m.id });
        const area = el('textarea', { class: 'input cleaner-copy-text', readonly: true, rows: '12' });
        area.value = result.text;
        const copy = button(tr("Копировать"), { class: 'btn btn-accent' }, async () => {
          try {
            await navigator.clipboard.writeText(result.text);
            copy.textContent = tr("Скопировано");
          } catch { copy.textContent = tr("Не вышло"); }
        });
        openDialog(root, { id: 'cleaner-copy-dialog', title: tr("Очищенная копия"), body: [area,
          el('p', { class: 'muted small' }, tr("Проверьте текст: распознавание может пропустить чувствительные данные."))],
        actions: [copy], cancelText: tr("Закрыть") });
      } catch (err) { notice.textContent = words(err); }
    });
  }

  /* «Что получила модель» ответа через шлюз Cleaner: последний запрос хода со стороны провайдера — с заменами.
     Studio текст не хранит, а просит у Cleaner (cleaner_route.sent); окно — только текстом, без разметки */
  function cleanerSent(m) {
    return button(tr("Что получила модель"), { class: 'btn btn-sm msg-cleaner-sent' }, async (event) => {
      const own = event.currentTarget;
      own.disabled = true;
      try {
        const sent = await api('GET', `/api/chats/${chatId}/messages/${encodeURIComponent(m.id)}/cleaner-sent`);
        const roles = { user: tr("Пользователь"), assistant: tr("Помощник"), tool: tr("Инструмент") };
        // системная часть общего подключения учётке не показывается (`hidden`): в ней профиль администратора
        const items = sent.messages.map((message) => {
          if (message.hidden) {
            return el('p', { class: 'muted small cleaner-sent-hidden' }, tr("Системная часть общего подключения скрыта"));
          }
          if (message.role === 'system') {
            return el('details', {}, el('summary', {}, tr("Инструкция модели")), el('pre', {}, message.content));
          }
          const calls = message.tool_calls?.length
            ? el('details', { class: 'cleaner-sent-calls' },
              el('summary', {}, tr("Вызовы инструментов ({0})", message.tool_calls.length)),
              el('pre', {}, JSON.stringify(message.tool_calls, null, 2))) : null;
          const callId = message.tool_call_id
            ? el('div', { class: 'muted small' }, tr("Идентификатор вызова: {0}", message.tool_call_id)) : null;
          const role = roles[message.role] || message.role;
          return el('section', {}, el('b', {}, message.name ? `${role} (${message.name})` : role),
            callId, message.content ? el('pre', {}, message.content) : null, calls);
        });
        // отказ Cleaner до провайдера: в журнале запроса нет сообщений
        if (!items.length) items.push(el('p', { class: 'muted small' }, tr("До провайдера запрос не дошёл.")));
        const counts = cleanerCounts(sent.counts);
        openDialog(root, { id: 'cleaner-sent-dialog', title: tr("Что получила модель"), cancelText: tr("Закрыть"), body: [
          el('p', {}, counts ? tr("Замены: {0}", counts) : tr("Автоматических замен нет.")),
          sent.tools ? el('p', { class: 'muted small' }, tr("Описаний инструментов: {0}", sent.tools)) : null,
          el('div', { class: 'cleaner-preview-text cleaner-sent-text' }, ...items),
          el('p', { class: 'muted small' }, tr("Последний запрос хода так, как его получил провайдер: с заменами Cleaner. Studio этот текст не хранит.")),
        ].filter(Boolean) });
      } catch (err) {
        notice.textContent = words(err);
      } finally {
        own.disabled = false;
      }
    });
  }

  function closeEdit() {
    if (!editing) return;
    input.value = editing.previous;
    skills.restore(editing.previousSkill);         // чип, выбранный до «Изменить»
    editing = null;
    editNote.hidden = true;
    autosize();
    ring.schedule();
  }

  function editTurn(m) {
    if (sending || jobs.has(chatId) || remoteStream() || otherTab()) return;
    if (talking) talking();
    closeEdit();
    editing = { message: m, last: chat.messages.at(-1).id, previous: input.value, previousSkill: skills.chosen() };
    input.value = humanText(m);
    skills.keep(m.skill);                          // навык сообщения — чипом; крестик — правка без навыка
    autosize();
    editNote.hidden = false;
    ring.hide();
    input.focus();
  }

  /* «Продолжить иначе» (chat_fork.py): новый диалог с историей до сообщения `m`, его текст — в поле (openWith);
     вложения самого сообщения ветка не получает — слова об этом в #chat-error, когда ветка откроется */
  async function forkAt(m) {
    const id = chatId;
    try {
      const out = await api('POST', `/api/chats/${id}/fork`, { message_id: m.id });
      if (!alive) return;
      if (chatId !== id) { loadList(); return; }       // человек уже ушёл в другой диалог — ветка ждёт в списке
      forkNote = (m.attachments || []).length ? { id: out.chat_id, text: tr("Вложения сообщения в новый диалог не перенесены") } : null;
      openWith(out.chat_id, out.text);
    } catch (err) {
      if (chatId === id) notice.textContent = words(err);
    }
  }

  function askDeleteTurn(m) {
    if (sending || jobs.has(chatId) || remoteStream() || otherTab()) return;
    const id = chatId;
    const remove = button(tr("Удалить ход"), { class: 'btn btn-danger msg-delete-confirm' });
    const dialog = openDialog(root, { title: tr("Удалить ход?"), body: [
      el('p', {}, tr("Сообщение и ответ будут удалены. Следующие ходы останутся."))], actions: [remove] });
    remove.addEventListener('click', async () => {
      remove.disabled = true;
      try {
        await api('DELETE', `/api/chats/${id}/turns/${m.id}`);
        dialog.close();
        if (chatId !== id) return;
        if (editing?.message.id === m.id) closeEdit();
        await reload();
        loadList();
      } catch (err) {
        dialog.close();
        if (chatId === id) notice.textContent = words(err);
      }
    });
  }

  /* Варианты ответа: «Ответить заново» оставляет прежний ответ невыбранным вариантом — у последнего ответа
     переключатель «‹ 1 / 2 ›» (`m.variants` с сервера, подпись модели — строкой под ответом) и на широком экране
     «Сравнить» — все варианты колонками (`comparing` — диалоги, где сравнение открыто). Выбор — POST …/choose:
     выбранный вариант уходит в следующий ход, поиск, «Поделиться» и экспорт; новый ход фиксирует выбор. */
  const comparing = new Set();

  async function chooseVariant(id, focus) {
    const at = chatId;
    try {
      await api('POST', `/api/chats/${at}/messages/${encodeURIComponent(id)}/choose`);
    } catch (err) {
      if (chatId === at) notice.textContent = words(err);
    }
    if (chatId !== at) return;
    await reload();
    if (focus) feed.querySelector(`.msg[data-id="${CSS.escape(id)}"] ${focus}`)?.focus();
  }

  function variantSwitch(m, variants) {
    const p = parts.get(m);
    const at = variants.findIndex((v) => v.id === m.id);
    const busy = sending || jobs.has(chatId) || remoteStream() || otherTab();
    const step = (by, cls) => {
      const label = by < 0 ? tr("Предыдущий вариант") : tr("Следующий вариант");
      const go = iconButton(by < 0 ? 'prev' : 'next', label, { class: `icon-btn ${cls}`, disabled: busy });
      go.addEventListener('click', () => chooseVariant(variants[(at + by + variants.length) % variants.length].id,
        `.${cls}`));
      return go;
    };
    const box = el('span', { class: 'msg-variant', role: 'group',
      'aria-label': tr("Вариант {0} из {1}", at + 1, variants.length) },
    step(-1, 'msg-variant-prev'), el('span', { class: 'msg-variant-at' }, `${at + 1} / ${variants.length}`),
    step(1, 'msg-variant-next'));
    const open = comparing.has(chatId);
    const toggle = button(open ? tr("Скрыть сравнение") : tr("Сравнить"), { class: 'btn btn-sm msg-compare-toggle',
      'aria-pressed': String(open) }, () => {
      if (comparing.has(chatId)) comparing.delete(chatId);
      else comparing.add(chatId);
      paint(m);
      p.actions.querySelector('.msg-compare-toggle')?.focus();
    });
    return [box, toggle];
  }

  /* сравнение: колонки «Вариант N · модель» с текстом каждого; не выбранный — «Выбрать этот вариант» */
  function paintCompare(p, m, variants) {
    const on = !!variants && comparing.has(chatId);
    p.node.classList.toggle('comparing', on);
    if (!on) {
      p.compare.replaceChildren();
      return;
    }
    const busy = sending || jobs.has(chatId) || remoteStream() || otherTab();
    p.compare.replaceChildren(...variants.map((v, i) => el('section', {
      class: `msg-compare-item${v.id === m.id ? ' chosen' : ''}`, 'data-id': v.id,
      'aria-label': tr("Вариант {0} из {1}", i + 1, variants.length) },
    el('p', { class: 'msg-compare-head small' }, tr("Вариант {0} · {1}", i + 1, v.model || '—')),
    v.content ? el('div', { class: 'md' }, renderMarkdown(v.content, { highlight: true }))
      : el('p', { class: 'muted small' }, v.error ? message(v.error) : tr("ответ не получен")),
    v.id === m.id ? el('p', { class: 'muted small msg-compare-chosen' }, tr("Выбран"))
      : button(tr("Выбрать этот вариант"), { class: 'btn btn-sm msg-compare-choose', disabled: busy },
        () => chooseVariant(v.id, '.msg-compare-toggle')))));
  }

  function messageNode(m, last) {
    if (m.role === 'user') {
      const box = el('div', { class: 'msg user', 'data-id': m.id });
      if (m.attachments && m.attachments.length) box.append(el('div', { class: 'atts' }, ...m.attachments.map(attLabel)));
      const { head, text } = skillMessage(m);      // навык — свёрнутым «Навык: имя», в пузыре — уточнение
      if (head) box.append(head);
      if (text) box.append(el('div', { class: 'bubble' }, text));
      if (m.id) {
        const actions = el('div', { class: 'msg-actions' },
          button(tr("Изменить"), { class: 'btn btn-sm msg-edit' }, () => editTurn(m)),
          button(tr("Продолжить иначе"), { class: 'btn btn-sm msg-fork',
            title: tr("Новый диалог с историей до этого сообщения, его текст — в поле") }, () => forkAt(m)),
          button(tr("Удалить ход"), { class: 'btn btn-sm msg-delete-turn' }, () => askDeleteTurn(m)));
        if (chat?.chat.cleaner && (m.content || (m.attachments || []).some((a) => a.kind === 'document'))) {
          actions.append(cleanerCopy(m));
        }
        box.append(actions);
      }
      return box;
    }
    const md = el('div', { class: 'md' });
    const meta = el('p', { class: 'msg-meta' });
    const model = el('p', { class: 'msg-model muted small', hidden: true });
    const actions = el('div', { class: 'msg-actions' });
    const visibleTools = m.tools?.filter((step) => !m.pending || !step.waiting) || [];
    const steps = visibleTools.length ? sourcesNode(visibleTools, m.status !== 'streaming') : null;
    sourceClicks(steps, { chatId, message: m, host: root });   // куски «Знаний» — окном с текстом
    const badge = el('button', { type: 'button', class: 'web-badge', hidden: true });
    const think = el('div', { class: 'think-box' });
    badge.addEventListener('click', () => {                    // «Источники» этого ответа — там тексты ошибок
      if (!steps) return;
      steps.open = true;
      steps.scrollIntoView({ block: 'nearest',
        behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
    });
    // ответ ждёт карту (503 шлюза): «Не ждать» — один узел на сообщение, перерисовка счётчика его не пересоздаёт
    const nowait = button(tr("Не ждать"), { class: 'btn btn-sm msg-nowait', hidden: true }, async () => {
      nowait.disabled = true;
      try {
        await api('POST', `/api/chats/${chatId}/nowait`);
      } catch (err) {
        if (err.code !== 'state') notice.textContent = words(err);   // state — уже не ждёт
      } finally {
        nowait.disabled = false;
      }
    });
    const consent = el('div', { class: 'msg-consents' });
    const pending = el('div', { class: 'msg-pending' });
    const plan = el('div', { class: 'msg-todo' });
    const compare = el('div', { class: 'msg-compare' });
    const node = el('div', { class: 'msg assistant', 'data-id': m.id }, el('span', { class: 'mark', 'aria-hidden': 'true' }),
      el('div', { class: 'msg-body' }, badge, think, plan, md, compare, pending, resultFiles(chatId, m, root),
        resultImages(m, showPhoto), consent, steps,
        model, meta, nowait, actions));
    parts.set(m, { badge, steps, think, thought: '', said: '', md, meta, nowait, actions, last, shown: null, consent, consentKey: '',
      pending, plan, model, planKey: '', node, compare });
    paint(m);
    return node;
  }

  /* диалог с сервера: сообщение, пришедшее тем же, что в показанном диалоге, остаётся прежним объектом — renderFeed
     не пересоздаёт его узел (опрос идущего ответа не сбивает фокус, выделение и нажатие в прежних сообщениях) */
  function settle(data) {
    const before = chat && chat.chat.id === data.chat.id ? chat.messages : [];
    const prior = new Map(before.filter((m) => received.has(m)).map((m) => [m.id, m]));
    data.messages = data.messages.map((m) => {
      const text = JSON.stringify(m);
      const old = prior.get(m.id);
      if (old && received.get(old) === text) return old;
      received.set(m, text);
      return m;
    });
    return data;
  }

  /* плашка: идёт инструмент — он с крутилкой; есть шаги с сервера — итог зелёным или красным; вызовы этого хода
     до перечитывания — итог без цвета (ошибок ещё не знаем); нет ничего — плашки нет (ответ по памяти) */
  function paintBadge(p, m, job) {
    const recorded = answerSteps(m);            // только авто-шаг «Знаний» в `tools` — живые `calls` хода
    const tools = recorded.length && recorded === m.tools ? m.tools : null;
    const steps = recorded.filter((step) => !m.pending || !step.waiting);
    // скан: своя вкладка — событие `ocr`, ответ, который читает не она, — `ocr` сообщения с сервера
    const scan = m.status === 'streaming' ? (job ? job.scan : m.ocr) : null;
    const busy = m.status === 'streaming' && job && job.tool;
    p.badge.hidden = !busy && !scan && !steps.length;
    if (p.badge.hidden) return;
    if (busy || scan) {
      p.badge.className = 'web-badge busy';
      p.badge.replaceChildren(el('span', { class: 'spin', 'aria-hidden': 'true' }), busy
        ? `${TOOL_MARKS[job.tool.kind] || '🌐'} ${toolLine(job.tool)}` : scanLine(scan));
    } else {
      p.badge.className = `web-badge${tools ? (tools.some((s) => s.error) ? ' bad' : ' ok') : ''}`;
      p.badge.replaceChildren(badgeText(steps));
    }
    p.badge.disabled = !p.steps;
    p.badge.setAttribute('aria-label', p.steps ? tr("{0} — показать источники", p.badge.textContent) : p.badge.textContent);
  }

  /* строка рассуждения под плашкой: модель думает — «Думает… N с» с крутилкой и бегущим текстом (если он виден),
     свёрнутым до нескольких строк — нажатие (с клавиатуры — Enter или пробел) раскрывает; ответ с рассуждением —
     свёрнутое «💭 Рассуждала N с». Узел строится заново при смене вида (модель думает и после текста) и вместе с
     лентой (конец ответа, опрос), а раскрытое человеком остаётся раскрытым (`unfolded`). Текст — textContent:
     свёрнутому бегущему — хвост THINK_TAIL знаков, раскрытому — дописывается кусками (`p.said` — что уже в узле): не
     раскладывать заново 60 тыс. знаков каждые RENDER_MS */
  function paintThought(p, m, job) {
    const text = m.reasoning || '';
    const busy = m.status === 'streaming' && (job ? !!job.thinking : !m.content && !!text);
    const mode = busy ? 'busy' : text ? 'done' : '';
    const unfold = (on) => (on ? unfolded.add(m.id) : unfolded.delete(m.id));
    if (mode !== p.thought) {
      p.thought = mode;
      p.said = '';
      if (mode === 'busy') {
        const run = el('div', { class: 'think-text', role: 'button', tabindex: '0' });
        const fold = () => {
          const on = unfolded.has(m.id);
          run.classList.toggle('open', on);
          run.setAttribute('aria-expanded', String(on));
          run.title = on ? tr("Свернуть") : tr("Показать целиком");
        };
        const flip = () => {
          unfold(!unfolded.has(m.id));
          p.said = '';
          fold();
          paintThought(p, m, reading(m));
        };
        run.addEventListener('click', flip);
        run.addEventListener('keydown', (e) => {
          if (e.key !== 'Enter' && e.key !== ' ') return;
          e.preventDefault();
          flip();
        });
        fold();
        p.think.replaceChildren(el('div', { class: 'think busy' }, el('p', { class: 'think-line' },
          el('span', { class: 'spin', 'aria-hidden': 'true' }), el('span', { class: 'think-sec' })), run));
      } else if (mode === 'done') {
        const details = el('details', { class: 'think' }, el('summary', {}), el('div', { class: 'think-text' }));
        details.open = unfolded.has(m.id);
        details.addEventListener('toggle', () => unfold(details.open));
        p.think.replaceChildren(details);
      } else p.think.replaceChildren();
    }
    if (!mode) return;
    const box = p.think.querySelector('.think-text');
    const folded = busy && !unfolded.has(m.id);
    const shown = folded ? text.slice(-THINK_TAIL) : text;
    if (p.said !== shown) {
      if (p.said && shown.startsWith(p.said) && box.lastChild) box.lastChild.appendData(shown.slice(p.said.length));
      else box.textContent = shown;
      p.said = shown;
      if (folded) box.scrollTop = box.scrollHeight;     // свёрнутый — виден конец
    }
    if (busy) {
      const sec = job ? thinkSec(job) : m.reasoning_sec;
      p.think.querySelector('.think-sec').textContent = tr("Думает… {0} с", Math.max(0, Math.round(sec || 0)));
    } else p.think.querySelector('summary').textContent = tr("💭 Рассуждала {0} с", Math.max(1, Math.round(m.reasoning_sec || 0)));
  }

  function paint(m) {
    const p = parts.get(m);
    if (!p) return;
    const job = reading(m);
    paintBadge(p, m, job);
    paintThought(p, m, job);
    paintConsent(m);
    paintTodo(p, m.todo);
    paintPending(p.pending, m.status !== 'streaming' && m.pending ? m.pending : null,
      (action, answer) => answerPending(m, action, answer));
    const displayed = String((m.cleaner_show_raw ? m.cleaner_raw : m.content) ?? '');
    const final = m.status !== 'streaming';
    if (p.shown !== displayed || p.final !== final) {
      p.shown = displayed;
      p.final = final;
      // «Открыть» у блоков html/svg — только у перечитанного с сервера: отметка защищённого источника к этому мигу верна
      const id = chatId;
      const fileLinks = final ? previewLinks(resultPathLinks(id, m), { host: root, chatId: id,
        allowed: () => alive && id === chatId && received.has(m) && !chat?.chat.protected,
        failed: (err) => { notice.textContent = words(err); } }) : null;
      if (displayed.length <= 2000) p.md.replaceChildren(renderMarkdown(displayed, { highlight: final, fileLinks }));
      else if (!final) p.md.replaceChildren(el('pre', { class: 'code-stream-text' }, displayed));
      else renderMarkdownInto(p.md, displayed, {
        fileLinks,
        valid: () => p.md.isConnected && parts.get(m) === p && p.shown === displayed && p.final,
        update: (change) => { change(); scrollDown(false); },
      });
    }
    let text = '';
    let kind = '';
    // карта занята (503 шлюза): своя вкладка — событие `wait`, ответ, который читает не она, — `wait` с сервера
    const waiting = m.status === 'streaming' && (job ? job.wait != null : Number.isFinite(m.wait));
    p.nowait.hidden = !waiting;
    // очередь карты и оценка — в строке состояния (chat-status.js), под ответом — только время ожидания
    if (waiting) text = tr("жду карту · {0}", job ? elapsed(job.wait) : elapsed(Date.now() - m.wait * 1000));
    else if (m.status === 'streaming' && m.recovery) {
      text = m.recovery === 'summary' ? tr("Сжимаю историю диалога…") : tr("Продолжаю ответ…");
    } else if (m.status === 'streaming') {
      // инструмент — на плашке, модель думает — строкой рассуждения: модель уже загружена; скан — на плашке.
      // Грузит модель только локальная (`local` диалога), облачная — просто ещё молчит
      if (job) {
        text = job.tool || job.scan || job.first || job.thinkFrom ? ''
          : (chat?.chat.local ? tr("загружаю модель · {0}", elapsed(job.started)) : tr("жду ответа · {0}", elapsed(job.started)));
      }
      else if (m.via === 'browser') text = m.live ? tr("ответ идёт в другой вкладке…") : tr("прервано");
      else text = m.ocr ? '' : tr("ответ ещё идёт…");
    } else if (m.status === 'stopped') text = tr("остановлено");
    else if (m.status === 'error') {
      text = m.error ? message(m.error) : m.content ? tr("ответ оборвался") : tr("ответ не получен");
      kind = ' error';
    }
    p.meta.textContent = text;
    p.meta.className = `msg-meta${kind}`;
    p.model.hidden = !m.model || m.status === 'streaming';
    if (!p.model.hidden) p.model.textContent = withSpeed(m.cleaner_raw != null || m.cleanerTurn || m.cleaner_sent
      ? tr("{0} · через Cleaner", m.model) : m.model, m);
    const variants = m.status !== 'streaming' && m.variants?.length > 1 ? m.variants : null;
    paintCompare(p, m, variants);
    p.actions.replaceChildren();
    if (m.status === 'streaming' && (job || m.via !== 'browser' || m.live)) return;   // «прервано» можно переспросить
    if (variants) p.actions.append(...variantSwitch(m, variants));
    if (m.content) {
      const copy = button(tr("Копировать"), { class: 'btn btn-sm msg-copy' }, async () => {
        try {
          await navigator.clipboard.writeText(displayed);
          copy.textContent = tr("Скопировано");
        } catch {
          copy.textContent = tr("Не вышло");
        }
        setTimeout(() => { copy.textContent = tr("Копировать"); }, COPIED_MS);
      });
      // «Прочитать» — источником «Озвучки» по умолчанию; проигрыватель — под кнопками, перерисовка его не трогает
      const read = button(tr("Прочитать"), { class: 'btn btn-sm msg-read' }, () => {
        if (!p.read?.isConnected) {
          p.read = el('div', { class: 'msg-read' });
          p.actions.after(p.read);
        }
        readAloud(displayed, { kind: 'chat', id: String(m.id || chatId) }, p.read);
      });
      p.actions.append(copy, read);
      // защищённый источник: ответ не озвучить (409 `protected`, speeches.PROTECTED_READ) — кнопки нет
      read.hidden = !!chat?.chat.protected;
    }
    const kept = unsaved.get(m.id);
    if (kept?.message === m && !kept.closed) {
      const again = button(tr("Сохранить ещё раз"), { class: 'btn btn-sm msg-save' }, async () => {
        again.disabled = true;
        const saved = await saveAnswer(kept.chatId, m, kept.body);
        paint(m);
        loadList();
        // перечитанная после сбоя лента держит строку сервера `streaming` с признаком жизни: без `unsaved` она — «ответ
        // в другой вкладке» (`otherTab`), пока ленту не перечитать
        if (saved) reload();
      });
      p.actions.append(again);
    }
    if (m.cleaner_raw != null) p.actions.append(button(m.cleaner_show_raw ? tr("Восстановленный ответ") : tr("Ответ модели"),
      { class: 'btn btn-sm msg-cleaner-view' }, () => { m.cleaner_show_raw = !m.cleaner_show_raw; paint(m); }));
    if (chat?.chat.cleaner && m.content) p.actions.append(cleanerCopy(m));
    if (m.cleaner_sent && m.id) p.actions.append(cleanerSent(m));
    if (lengthError(m)) p.actions.append(offer.button());   // ответ не поместился — «Новый диалог с итогом»
    if (p.last) p.actions.append(button(tr("Ответить заново"), { class: 'btn btn-sm msg-retry' }, () => submit(true)));
  }

  function renderFeed() {
    if (!chat) return;
    const msgs = chat.messages.map(liveOf);
    if (!msgs.length) {
      feed.replaceChildren(emptyNote());
      return;
    }
    const cleaner = !!chat.chat.cleaner;               // от него — «Очищенная копия» ответов
    keep(feed, placeSummary(msgs.map((m, i) => {
      const last = i === msgs.length - 1;
      const was = shown.get(m);
      // идущий ответ рисуется заново: «жду карту · м:сс» считается от неизменного `wait` по Date.now()
      if (was && was.last === last && was.cleaner === cleaner && received.has(m) && m.status !== 'streaming') {
        return was.node;
      }
      const node = messageNode(m, last);
      shown.set(m, { node, last, cleaner });
      return node;
    }), msgs, chat));
    paintConsents();                                   // решение о файлах MCP могло смениться и у прежних узлов
    nav.paint();
  }

  function emptyNote() {
    const suggest = el('div', { class: 'chat-suggest', id: 'chat-suggest', role: 'group', 'aria-label': tr("Подсказки"),
      hidden: true });
    paintSuggest(suggest);
    return el('div', { class: 'empty chat-empty' }, el('span', { class: 'badge', 'aria-hidden': 'true' }, icon('chat')),
      el('p', {}, tr("Напишите сообщение или прикрепите документ — PDF, DOCX, TXT, MD или фото")), suggest);
  }

  function scrollDown(force) {
    if (force) {
      stick = true;
      found = null;
    }
    if (found) reveal();
    else if (stick) scroller.scrollTop = scroller.scrollHeight;
  }

  /* сообщение из поиска: его верх — у верха ленты. Длинные ответы выше него дорисовываются кадрами (markdown.js) —
     каждая дорисовка зовёт scrollDown, и оно снова ставится на место; первое касание ленты отпускает. Нет такого
     сообщения (удалено, другой диалог) — конец ленты, как обычно. */
  function reveal() {
    const node = [...feed.children].find((n) => n.dataset.id === found.id);
    if (!node) {
      scrollDown(true);
      return;
    }
    stick = false;
    if (!found.lit) {
      found.lit = true;
      node.classList.add('msg-found');
    }
    scroller.scrollTop += node.getBoundingClientRect().top - scroller.getBoundingClientRect().top - REVEAL_PX;
  }

  function remoteStream() {
    return !!chat && chat.messages.some((m) => m.role === 'assistant' && m.status === 'streaming' && m.via === 'server'
      && liveOf(m) === m);
  }

  /* ответ из браузера, который читает другая вкладка: остановить его отсюда нельзя, только дождаться */
  function otherTab() {
    return !!chat && chat.messages.some((m) => m.role === 'assistant' && m.status === 'streaming' && m.via === 'browser'
      && m.live && liveOf(m) === m);
  }

  function summaryBusy() {
    return sending || jobs.has(chatId) || remoteStream() || otherTab();
  }

  function syncBusy() {
    const busy = jobs.has(chatId) || remoteStream();
    editNote.querySelector('button').disabled = sending;
    send.hidden = busy;
    stop.hidden = !busy;
    queueAdd.hidden = !(busy || otherTab());
    offer.sync();
    ctxWindow.sync();
    paintGuard();
    status.paint();
    // после текущего шага: конец ответа следом перечитывает ленту (refresh), и очередь ждёт её
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => { if (alive) queue.idle(chatId, queueFree()); }, 0);
  }

  /* очередь уходит, когда ответ кончился, свой ход не отправляется, нет правки и ждущей карточки, лента перечитана */
  function queueFree() {
    return !jobs.has(chatId) && !remoteStream() && !otherTab() && !sending && !editing && !historyRead && !!chatId
      && chat?.chat.id === chatId && !chat.messages.some((m) => liveOf(m).pending);
  }

  function schedulePoll() {
    clearTimeout(poll);
    poll = null;
    if (!alive || document.visibilityState === 'hidden' || navigator.onLine === false) return;
    if (needsReload) {
      if (failures < RECOVERY_DELAYS.length) poll = setTimeout(reload, RECOVERY_DELAYS[failures]);
    } else if ((remoteStream() || otherTab()) && !jobs.has(chatId)) poll = setTimeout(reload, POLL_MS);
  }

  /* отправка принята: она уходит из поля, даже если открыт уже другой диалог (поле общее) — чипы по самим
     объектам, текст — только тот же, что ушёл (набранное заново остаётся) */
  function acknowledgeTurn(attempt) {
    if (pendingTurn === attempt) pendingTurn = null;
    if (attempt.regenerate || attempt.acknowledged) return;
    attempt.acknowledged = true;
    // сообщение очереди принято — из очереди уходит; поле и черновик — набранное человеком, их не трогаем
    if (attempt.queued) queue.done(attempt.queued);
    inputDraft.sent(attempt.text);                     // ушли из раздела до подтверждения — черновиком не вернётся
    if (!attempt.queued && !attempt.edit) dialogDrafts.sent(attempt.id, attempt.text, attempt.skill);
    if (attempt.edit) {
      if (editing?.message.id === attempt.edit.id && chatId === attempt.id) closeEdit();
    } else if (!attempt.queued && input.value === attempt.text) { input.value = ''; autosize(); }
    if (attempt.skill && skills.chosen() === attempt.skill) skills.clear();   // навык ушёл с ходом
    if (!attempt.queued) skills.close();               // меню «/» — после отправки хода (slash.js)
    atts = atts.filter((a) => !attempt.sent.includes(a));
    paintAtts();
  }

  async function reload() {
    if (!alive || !chatId || document.visibilityState === 'hidden') return;
    const id = chatId;
    const mine = opened;
    if (historyRead && historyRead.mine === mine) return;
    const own = { mine, controller: new AbortController() };
    historyRead = own;
    const timeout = setTimeout(() => own.controller.abort(), 15000);
    const attempt = pendingTurn?.id === id && pendingTurn.uncertain ? pendingTurn : null;
    try {
      const options = { signal: own.controller.signal };
      const [data, receipt] = await Promise.all([
        api('GET', `/api/chats/${id}`, undefined, options),
        attempt ? api('GET', `/api/chats/${id}/turns/${attempt.requestId}`, undefined, options) : null,
      ]);
      if (!alive || mine !== opened || chatId !== id) return;
      chat = settle(data);
      if (chat.chat.project_id && chat.chat.project_id !== projectId) {
        projectId = chat.chat.project_id;
        paintProjects();
        if (!currentProject() || currentProject().id !== projectId) loadProjects();
      }
      shareContext();
      if (receipt?.accepted && data.messages.some((message) => message.id === receipt.assistant_message_id)) {
        acknowledgeTurn(attempt);
      }
      // сообщение очереди сервер не принимал: оно ждёт «Продолжить очередь» (тот же `request_id`), квитанцию больше
      // не спрашиваем. Запоздалая квитанция (повтор «Продолжить очередь» уже идёт — `pendingTurn` новый) — не трогаем
      const unsent = receipt && !receipt.accepted && attempt.queued && pendingTurn === attempt;
      if (unsent) pendingTurn = null;
      needsReload = !!(pendingTurn?.id === id && pendingTurn.uncertain);
      failures = needsReload ? failures + 1 : 0;
      notice.textContent = '';
      if (unsent) queue.unsure(attempt.queued);
    } catch (err) {
      if (!alive || mine !== opened) return;
      notice.textContent = words(err);
      needsReload = err.status !== 404 && err.code !== 'request_gone';
      failures++;
      schedulePoll();
      return;
    } finally {
      clearTimeout(timeout);
      if (historyRead === own) historyRead = null;
    }
    renderFeed();
    paintChip();
    syncBusy();
    schedulePoll();
    scrollDown(false);
  }

  async function open(id, restore = false, requested = null) {
    panel(false);
    models.close();
    roles.close();
    if (!ctx.hint || id !== null) focusDesktop(input);
    const newProject = id === null && requested && !requested.startsWith('inbox-') ? requested : null;
    if (id === chatId && (id === null ? newProject === draftProject : chat)) return;
    find.reset();
    dropPreviews(root);
    root.querySelector('#project-file-picker[open]')?.close();
    root.querySelector('#kb-source-dialog[open]')?.close();
    quote.clear();
    closeEdit();
    unqueueField();                                  // в поле — набранное, а не текст уходящего сообщения очереди
    if (chatId !== undefined) dialogDrafts.leave(chatId);   // первое открытие — черновика прежнего диалога нет
    if (talking) talking();                          // «Разговор» — у своего диалога, другой его останавливает
    const mine = ++opened;
    chatId = id;
    draftProject = newProject;
    projectId = id ? listed.find((item) => item.id === id)?.project_id : requested;
    paintProjects();
    lastChat = id;
    dialogDrafts.enter(id);
    queue.show(id);
    found = null;                                    // ?m= нового адреса ставит query() следом
    shareContext();
    chat = null;
    ring.hide();                                     // кольцо прежнего диалога — до оценки этого
    suggestKey = null;                               // подсказки — заново: профиль и MCP могли смениться
    needsReload = false;
    failures = 0;
    historyRead?.controller.abort();
    historyRead = null;
    draftWeb = false;
    draftMcp = null;
    draftThink = false;
    draftRole = null;
    draftEffort = null;
    offer.hide();
    draftKbs = [];
    pendingMedia = [];
    clearTimeout(poll);
    stopWaiting();                                  // ушли из диалога — скан больше не ждём
    notice.textContent = '';
    let inList = false;
    for (const link of list.querySelectorAll('.chat-link')) {
      const here = link.closest('.chat-item').dataset.id === id;
      inList ||= here;
      if (here) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    if (id !== null && !inList) {                    // диалог новее списка (другая вкладка, ссылка)
      firstList.then(() => {                         // первый список раздела ещё мог не прийти
        if (chatId === id && ![...list.querySelectorAll('.chat-item')].some((n) => n.dataset.id === id)) loadList();
      });
    }
    syncBusy();
    paintChip();
    form.hidden = id === null && !!ctx.hint && !mcpModelServers.length;         // без модели новый диалог не начать; прежний — по правилам сервера
    if (id === null) {
      feed.replaceChildren(form.hidden ? ctx.hint() : emptyNote());
      return;
    }
    feed.replaceChildren(el('p', { class: 'muted feed-note' }, tr("Загружаю диалог…")));
    try {
      const data = await api('GET', `/api/chats/${encodeURIComponent(id)}`);
      if (!alive || mine !== opened) return;
      chat = settle(data);
      projectId = chat.chat.project_id;
      paintProjects();
      shareContext();
      if (prefill?.id === id) {                      // openWith: текст — в поле открытого диалога
        input.value = prefill.text;
        autosize();
        ring.schedule();
        dialogDrafts.typed(id);                      // и в его черновик: возврат из bfcache его не сотрёт
      }
    } catch (err) {
      if (prefill?.id === id) prefill = null;       // текст openWith не ждёт следующего открытия этого диалога
      if (!alive || mine !== opened) return;
      if (restore && err.status === 404) {
        history.replaceState(history.state, '', '#/chat');
        open(null);
        return;
      }
      feed.replaceChildren(el('p', { class: 'error feed-note' }, words(err)));
      needsReload = err.status !== 404;
      schedulePoll();
      return;
    }
    if (prefill?.id === id) prefill = null;
    if (forkNote?.id === id) {                       // ветка от сообщения с вложениями
      notice.textContent = forkNote.text;
      forkNote = null;
    }
    renderFeed();
    paintChip();
    syncBusy();
    schedulePoll();
    scrollDown(!found);                              // из поиска — к сообщению, иначе — в конец
  }

  /* поле ввода и вложения */
  function paintAtts() {
    chipsBox.replaceChildren(...atts.map((a) => {
      const name = a.file ? a.file.name : a.meta.name;
      const image = a.file ? a.file.type.startsWith('image/') : a.meta.kind === 'image';
      const remove = iconButton('x', tr("Убрать {0}", name), { class: 'icon-btn att-remove' });
      remove.addEventListener('click', () => {
        atts = atts.filter((x) => x !== a);
        dropWaiting();
        paintAtts();
        // уже загружено, но не отправлено — и с сервера; отказ не важен: остаток уберёт уборка
        if (a.meta) api('DELETE', `/api/attachments/${a.meta.id}`).catch(() => {});
      });
      let label = el('span', { class: 'att-name' }, name);
      if (a.transcript || a.scan) {                  // весь передаваемый текст — по нажатию
        label = el('button', { type: 'button', class: 'att-name att-open', title: tr("Показать текст") }, name);
        label.addEventListener('click', () => showText(a));
      }
      return el('span', { class: `att-chip${a.error ? ' bad' : ''}` }, icon(image ? 'photo' : 'doc'), label,
        a.transcript || a.scan ? el('span', { class: 'muted att-state' }, tr("{0} зн.", a.meta.chars.toLocaleString(locale()))) : null,
        a.busy ? el('span', { class: 'muted att-state' }, tr("загружаю…")) : null,
        a.error ? el('span', { class: 'error att-state' }, a.error) : null,
        sending ? null : remove);
    }));
    paintGuard();
    ring.schedule();
  }

  function autosize() {
    input.style.height = 'auto';
    // Пустое поле сохраняет три строки из общих стилей, непустое растёт по содержимому.
    if (input.value) input.style.height = `${input.scrollHeight}px`;
  }

  /* файл не подойдёт — слова сразу при выборе, а не после «Отправить»: тип по расширению и типу файла, размер —
     пределом сервера (фото больше 16 Мп и битый файл скажет сервер). Не узнать (ни типа, ни расширения) — решит сервер */
  function unfit(f) {
    const ext = /\.[^.]*$/.exec(f.name.toLowerCase())?.[0] || '';
    const doc = DOC_EXT.includes(ext);
    const photo = !doc && (PHOTO_TYPES.includes(f.type) || PHOTO_EXT.includes(ext));
    if (!doc && !photo && (ext || f.type)) return tr("поддерживаются PDF, DOCX, текстовые файлы и код, фото PNG, JPEG, WebP");
    if (photo && f.size > PHOTO_MAX) return tr("фото больше {0} МиБ", PHOTO_MAX >> 20);
    if (f.size > DOC_MAX) return tr("файл больше {0} МиБ", DOC_MAX >> 20);
    return '';
  }

  async function upload(a, id) {
    if (a.meta) return true;
    a.busy = true;
    a.error = '';
    paintAtts();
    try {
      const body = new FormData();
      body.append('file', a.file, a.file.name);
      a.meta = await api('POST', '/api/attachments', body, { headers: { 'X-Studio-Chat': id } });
      return true;
    } catch (err) {
      a.error = words(err);
      return false;
    } finally {
      a.busy = false;
      paintAtts();
    }
  }

  /* 409 `not_ready`: раз в NOT_READY_MS — документы сканов поля ввода (`ocr` ответа загрузки) и вложений
     сообщений диалога (`ocr` идущего скана, ход с неготовым сканом из истории) — только состояние (`brief`);
     пока какой-то идёт — ждём, затем ход заново (ни одного id — просто ход заново). Смена диалога, уход из
     раздела и «Отправить» человека ожидание снимают; правка поля и вложений — только у обычного хода
     (dropWaiting): по таймеру ушло бы не то, что человек отправлял, а повтор поле и вложения не берёт */
  function waitScans(args) {
    const mine = { timer: 0, regenerate: args[0] };
    notReady = mine;
    const step = async () => {
      const ids = [...atts.map((a) => a.meta && a.meta.ocr && a.meta.ocr.id),
        ...(chat ? chat.messages : []).flatMap((m) => (m.attachments || []).map((a) => a.ocr && a.ocr.id))]
        .filter(Boolean);
      try {
        for (const id of ids) {
          const { state } = await api('GET', `/api/ocr/${encodeURIComponent(id)}?brief=1`);
          if (notReady !== mine) return;
          if (SCAN_RUNNING.includes(state)) {
            mine.timer = setTimeout(step, NOT_READY_MS);
            return;
          }
        }
      } catch {
        // документа нет или сеть — решит ход
      }
      if (notReady === mine) submit(...args);
    };
    mine.timer = setTimeout(step, NOT_READY_MS);
  }

  function stopWaiting() {
    if (notReady) clearTimeout(notReady.timer);
    notReady = null;
  }

  function dropWaiting() {
    if (!notReady || notReady.regenerate) return;
    stopWaiting();
    if (notice.textContent === NOT_READY) notice.textContent = '';
  }

  async function submit(regenerate, externalOk = false, webOk = false) {
    // ответ идёт (здесь или через сервер с другого устройства): Enter ставит текст в очередь. Ответ другой вкладки
    // (`otherTab`) — только кнопкой «После ответа»: его признак у страницы бывает устаревшим, сервер решит сам
    if (!regenerate && !queuing && !voiceWait && chatId && (jobs.has(chatId) || remoteStream())) {
      enqueue();
      return null;
    }
    if (!regenerate && !queuing && !voiceWait) quote.fold();   // цитата — в начало поля (очередь и «Разговор» не трогаем)
    stopWaiting();                                 // ждали скан — ход идёт сейчас
    const text = input.value;
    const retained = editing?.message.attachments?.length || 0;
    if (sending || jobs.has(chatId) || (!regenerate && !text.trim() && !atts.length && !retained && !skills.chosen())) return;
    if (!regenerate && blocked()) return;
    if (!regenerate && atts.some((a) => a.unfit)) {   // слова — у чипа с выбора файла; диалог не создаётся
      notice.textContent = tr("Уберите неподходящие файлы");
      return;
    }
    if (!regenerate && atts.length + retained > ATTACH_MAX) {
      notice.textContent = tr("не больше {0} вложений в сообщении", ATTACH_MAX);
      return;
    }
    if (!externalOk && needsConsent(regenerate)) {
      askExternal(regenerate, webOk);
      return;
    }
    if (!webOk && needsWebConsent(regenerate)) {
      askWeb(regenerate, externalOk);
      return;
    }
    sending = true;
    notice.textContent = '';
    offer.hide();
    syncBusy();
    paintAtts();
    const made = {};                               // диалог, созданный этим ходом (`id`)
    let again = false;                             // ход повторится сам (окно подтверждения, скан): диалог нужен
    let refusal = null;                            // отказ хода — очереди (chat-queue.js): `busy` ждёт, прочее — пауза
    try {
      await turn(regenerate, text, externalOk, webOk, made);
    } catch (err) {
      refusal = err;
      again = ['external', 'external_web', 'not_ready'].includes(err.code);
      // сообщение очереди: подтверждение и ожидание скана повторили бы ход текстом поля — очередь встаёт на паузу
      if (queuing && again) notice.textContent = words(err);
      // вкладка не знала (другая вкладка сменила модель, включила «Интернет», ключ Exa появился)
      else if (err.code === 'external') askExternal(regenerate, webOk);
      else if (err.code === 'external_web') askWeb(regenerate, externalOk);
      else if (err.code === 'not_ready') {        // прямое подключение: скан ждёт страница
        notice.textContent = NOT_READY;
        waitScans([regenerate, externalOk, webOk]);
      } else if (err.code === 'busy') {           // ход начат на другом устройстве или вкладке — показать его
        await reload();                            // идущий ответ — в ленте, у сервера — с «Остановить»
        notice.textContent = words(err);
      } else if (!noKey(err)) notice.textContent = words(err);
    } finally {
      sending = false;
      syncBusy();
      paintAtts();
    }
    if (made.id && !again) dropEmpty(made.id);
    if (lengthRefusal(refusal) && chatId && chat?.messages.length) offer.show();   // есть что пересказать
    return refusal;
  }

  /* первый ход нового диалога не прошёл (отказ, отмена окна Cleaner, сбой загрузки вложения): созданный под него
     диалог без сообщений убирается — в списке не остаётся пустой «Новый диалог»; модель и кнопки диалога снова
     черновик нового, поле и вложения — как были. Ход принят или исход неясен (связь) — диалог остаётся; сервер
     удаляет только пустой (`?empty=1`), чего не убрали — уберёт его уборка через сутки */
  function dropEmpty(id) {
    if (jobs.has(id) || (pendingTurn?.id === id && pendingTurn.uncertain)) return;
    if (chatId === id) {
      if (!chat || chat.messages.length) return;
      const was = chat.chat;
      const knew = chat.knowledge || [];
      ++opened;
      chatId = null;
      dialogDrafts.rename(id, null);               // черновик снова у нового диалога
      queue.show(null);
      chat = null;
      draft = { connection_id: was.connection_id, model: was.model };
      draftWeb = !!was.web;
      draftMcp = draftOf(was, mcpServers);
      draftThink = !!was.thinking;
      draftRole = was.role || null;
      draftEffort = was.effort || null;
      draftKbs = knew;
      if (alive && location.hash === `#/chat/${id}`) {
        lastChat = null;
        history.replaceState(history.state, '', draftProject ? `#/chat?p=${draftProject}` : '#/chat');
      }
      shareContext();
      feed.replaceChildren(emptyNote());
      syncBusy();
      paintChip();
    }
    api('DELETE', `/api/chats/${encodeURIComponent(id)}?empty=1`).catch(() => {}).then(loadList);
  }

  /* диалог для хода или звука нового диалога: создать из черновика (модель, MCP, роль, «Рассуждение», «Интернет»);
     `made.id` — создал этот вызов. Идущее создание второй вызов ждёт, а не создаёт ещё один диалог */
  async function ensureChat(made) {
    if (chatId) return;
    if (creating) {
      await creating;
      return;
    }
    creating = createChat(made);
    try {
      await creating;
    } finally {
      creating = null;
    }
  }

  async function createChat(made) {
    const sourceHash = location.hash;
    const mine = opened;
    const created = await api('POST', '/api/chats', { ...draft, ...(draftProject ? { project_id: draftProject } : {}) });
    if (!alive || opened !== mine || location.hash !== sourceHash) {
      dropEmpty(created.id);
      throw new DOMException('', 'AbortError');
    }
    ++opened;
    chatId = created.id;
    dialogDrafts.rename(null, created.id);         // черновик нового диалога переходит к созданному
    queue.show(created.id);
    made.id = created.id;
    chat = { chat: created, messages: [], file_count: 0 };
    shareContext();
    draft = null;
    // ушли из раздела или в другой диалог, пока создавался этот, — адрес не трогать: ход дочитает «В работе»
    if (alive && location.hash === sourceHash) {
      lastChat = created.id;
      history.replaceState(history.state, '', `#/chat/${created.id}`);
    }
    loadList();
    const think = draftThink && thinkOk();     // «Рассуждение», включённое до создания диалога
    if (created.mcp_model_sid) {
      draftMcp = null;
      draftWeb = false;
    }
    if (draftMcp) {                              // MCP, выбранные до создания диалога; иначе — общие по умолчанию
      const body = draftMcp;
      draftMcp = null;
      chat.chat = await api('PATCH', `/api/chats/${created.id}`, body);
      paintWeb();
    }
    draftThink = false;
    if (draftRole) {                            // роль, выбранная до создания диалога
      const role = draftRole;
      draftRole = null;
      chat.chat = await api('PATCH', `/api/chats/${created.id}`, { role_id: role.id });
      roles.paint();
    }
    if (draftKbs.length) {                      // базы знаний, выбранные до создания диалога
      const ids = draftKbs;
      draftKbs = [];
      chat.knowledge = (await api('PUT', `/api/chats/${created.id}/knowledge`, { kb_ids: ids })).kb_ids;
      kbs.paint();
    }
    if (think) {
      chat.chat = await api('PATCH', `/api/chats/${created.id}`, { thinking: true });
      paintThink();
    }
    if (draftEffort) {                          // уровень рассуждения, выбранный до создания диалога
      const level = draftEffort;
      draftEffort = null;
      if (chat.chat.effort_ok) chat.chat = await api('PATCH', `/api/chats/${created.id}`, { effort: level });
      paintThink();
    }
    if (draftWeb && ['ready', 'mcp'].includes(webState())) {   // «Интернет», включённый до создания диалога
      draftWeb = false;
      chat.chat = await api('PATCH', `/api/chats/${created.id}`, { web: true });
      paintWeb();
    }
  }

  async function turn(regenerate, text, externalOk, webOk, made) {
    if (!chatId && ctx.hint && !draft?.mcp_model_sid) {
      throw new Error(tr("Выберите модель"));
    }
    if (chosen().mcp_model_sid && atts.length) {
      throw new Error(MCP_TEXT_CHAT);
    }
    await ensureChat(made);
    const id = chatId;
    const edited = regenerate ? null : editing;
    const sent = regenerate ? [] : atts.slice();
    for (const a of sent) {
      if (chatId !== id || !(await upload(a, id))) return;  // отказ — словами у чипа
    }
    if (chatId !== id) return;
    const edit = edited ? { id: edited.message.id, last_id: edited.last } : null;
    const turnInput = regenerate ? { regenerate: true } : { text, attachment_ids: sent.map((a) => a.meta.id),
      ...(edit ? { edit } : {}), ...skills.body() };
    const skill = regenerate ? null : skills.chosen();
    let cleanerTicket;
    if (chat.chat.cleaner) {
      let retry;
      let local = false;
      const rules = [];
      do {
        retry = false;
        notice.textContent = tr("Проверяю данные в Cleaner…");
        const shown = await api('POST', `/api/chats/${id}/cleaner-preview`,
          { ...turnInput, cleaner_rules: rules, cleaner_local: local });
        notice.textContent = '';
        if (chatId !== id) return;
        cleanerTicket = await new Promise((resolve) => {
          const confirm = button(local ? tr("Отправить оригиналы локальной модели") : tr("Отправить проверенный текст"), { class: 'btn btn-accent', id: 'cleaner-confirm',
            disabled: !shown.deliverable });
          const content = el('div', { class: 'cleaner-preview-text' }, ...shown.messages.map((m) =>
            m.role === 'system' ? el('details', {}, el('summary', {}, tr("Инструкция модели")), el('pre', {}, m.content))
              : el('section', {}, el('b', {}, m.role === 'user' ? tr("Пользователь") : tr("Помощник")), el('pre', {}, m.content))));
          const counts = cleanerCounts(shown.counts);
          const fragment = el('input', { class: 'input', id: 'cleaner-fragment', maxlength: '2000',
            placeholder: tr("Фрагмент исходного текста"), autocomplete: 'off' });
          const type = el('select', { class: 'input', id: 'cleaner-type' }, ...Object.entries({
            TERM: tr("Термин"), PERSON: tr("Человек"), ORG: tr("Организация"), SECRET: tr("Секрет"), EMAIL: tr("Почта"),
            PHONE: tr("Телефон"), ADDRESS: tr("Адрес"), HOST: tr("Узел"), ACCOUNT: tr("Учётная запись"),
          }).map(([value, name]) => el('option', { value }, name)));
          const edit = el('details', { id: 'cleaner-edit' }, el('summary', {}, tr("Исправить замены")),
            field(tr("Фрагмент"), fragment, 'fragment'), field(tr("Тип"), type, 'type'));
          let accepted = false;
          for (const [action, label] of [['mask', tr("Скрыть и перепроверить")], ['unmask', tr("Показать и перепроверить")]]) {
            edit.append(button(label, { class: 'btn btn-sm', id: `cleaner-${action}` }, () => {
              if (!fragment.value.trim()) { fragment.focus(); return; }
              rules.push({ action, value: fragment.value, type: type.value });
              retry = true;
              dialog.close();
            }));
          }
          const switchRoute = button(local ? tr("Вернуться к облачному предпросмотру") : tr("Выбрать локальную модель"), { class: 'btn', id: 'cleaner-local' }, () => {
            local = !local;
            retry = true;
            dialog.close();
          });
          const dialog = openDialog(root, { id: 'cleaner-preview-dialog', title: local ? tr("Оригиналы для локальной модели") : tr("Что получит модель"), body: [
            local ? el('p', {}, tr("Оригиналы получит {0} ({1}). Проверьте состав перед подтверждением.", shown.destination.model, shown.destination.url))
              : el('p', {}, counts ? tr("Замены: {0}", counts) : tr("Автоматических замен нет.")), content, local ? null : edit,
            el('p', { class: 'muted small' }, tr("Проверьте текст: распознавание может что-то пропустить. Правки замен действуют только в этом диалоге. Изображения через Cleaner не передаются.")),
            !shown.deliverable ? el('p', { class: 'error' }, tr("Отправка заблокирована политикой Cleaner.")) : null,
          ].filter(Boolean), actions: [switchRoute, confirm], cancelText: tr("Вернуться") });
          confirm.addEventListener('click', () => { accepted = true; dialog.close(); });
          dialog.addEventListener('close', () => resolve(accepted ? shown.ticket : null), { once: true });
        });
      } while (retry && chatId === id);
      // «Вернуться» у сообщения очереди: очередь встаёт на паузу (chat-queue.js) — со словами, почему
      if (queuing && !cleanerTicket && chatId === id) {
        notice.textContent = tr("Сообщение из очереди не отправлено — «Продолжить очередь» снова покажет, что получит модель");
      }
      if (!cleanerTicket || chatId !== id || (!regenerate && input.value !== text)) return;
    }
    const before = chat.messages;
    const user = regenerate ? null : { id: edit?.id || null, role: 'user', content: text, status: 'done', skill: skills.view(),
      attachments: [...(edited?.message.attachments || []), ...sent.map((a) =>
        (a.transcript ? { ...a.meta, source: { transcript_id: a.transcript, exists: true } } : a.meta))] };
    const message = { id: null, role: 'assistant', content: '', status: 'streaming', via: null, attachments: [],
      model: chat.chat.model, cleanerTurn: !!cleanerTicket };
    const job = { chatId: id, connId: chat.chat.connection_id, user, message, started: Date.now(), first: false,
      lost: false, controller: new AbortController() };
    let kept = before;
    if (edit) kept = before.slice(0, before.findIndex((m) => m.id === edit.id));
    else if (regenerate && before.at(-1)?.role === 'assistant') kept = before.slice(0, -1);
    chat.messages = [...kept, ...(user ? [user] : []), message];
    jobs.set(id, job);
    answering.textContent = tr("Модель отвечает…");
    renderFeed();
    syncBusy();
    scrollDown(true);
    let reply;
    const signature = JSON.stringify([id, turnInput]);
    if (queuing && !regenerate) {                  // сообщение очереди: его id — квитанция хода (повтор не задвоит)
      pendingTurn = { signature, requestId: queuing.item.id, queued: queuing.item.id, id, regenerate, text, sent, edit,
        skill };
    } else if (pendingTurn?.signature !== signature || pendingTurn.queued) {
      // черновик с квитанцией сообщения очереди (chat-drafts.js): тот же текст — тем же `request_id`, без задвоения
      const receipt = !regenerate && !edit && !sent.length && !skill ? dialogDrafts.receipt(id, text) : null;
      pendingTurn = { signature, requestId: receipt || crypto.randomUUID(), id, regenerate, text, sent, edit, skill };
    }
    const attempt = pendingTurn;
    try {
      reply = await api('POST', `/api/chats/${id}/turns`, {
        request_id: attempt.requestId,
        ...turnInput,
        ...(externalOk ? { external_ok: true } : {}), ...(webOk ? { web_ok: true } : {}),
        ...(cleanerTicket ? { cleaner_ticket: cleanerTicket } : {}) });
    } catch (err) {
      jobs.delete(id);
      answering.textContent = '';                  // отказ — словами в #chat-error
      const uncertain = [0, 502, 503, 504].includes(err.status) || err.name === 'TimeoutError';
      if (uncertain) attempt.uncertain = true;
      else if (pendingTurn === attempt) pendingTurn = null;
      if (chatId === id) {
        chat.messages = before;
        renderFeed();
        if (uncertain) { needsReload = true; failures = 0; schedulePoll(); }
      }
      throw err;
    }
    acknowledgeTurn(attempt);
    if (reply.replayed) {
      jobs.delete(id);
      answering.textContent = '';
      if (chatId === id) { chat.messages = before; needsReload = true; await reload(); }
      return;
    }
    if (chatId === id && externalOk) chat.chat.external = false;   // сервер сохранил согласие для получателя
    if (chatId === id && webOk) chat.chat.external_web = false;    // сервер записал время подтверждения
    loadList();
    if (voiceWait && !regenerate && voiceWait.text === text) {   // ход «Разговора»: текст ответа — в озвучку
      job.voice = voiceWait;
      voiceWait = null;
    }
    if (reply instanceof Response) {
      kick();                                      // ответ через сервер — в «В работе»
      readServer(job, reply);
    }
    else readBrowser(job, reply);
  }

  /* ответ на паузу сообщения `m` (кнопка карточки): то же сообщение снова идёт, поток SSE ответа — в его пузырь */
  async function answerPending(m, action, answer) {
    const id = chatId;
    const pending = m.pending;
    if (!id || !pending || jobs.has(id)) return;
    // ответ потерян в сети — повтор того же ответа с тем же request_id сервер узнает (replayed), а не 409 pending_gone
    const signature = JSON.stringify([id, pending.id, action, answer ?? null]);
    if (pendingAnswer?.signature !== signature) pendingAnswer = { signature, requestId: crypto.randomUUID() };
    let reply;
    try {
      reply = await api('POST', `/api/chats/${id}/pending/${pending.id}`, {
        request_id: pendingAnswer.requestId, action, ...(answer !== undefined ? { answer } : {}) });
    } catch (err) {
      if (chatId === id) {
        notice.textContent = words(err);
        if (['pending_gone', 'connection_changed', 'request_conflict'].includes(err.code)) reload();
      }
      return;
    }
    if (reply.replayed) {                           // ответ уже принят: идущее сообщение — из выдачи диалога
      if (chatId === id) { notice.textContent = ''; needsReload = true; await reload(); }
      return;
    }
    delete m.pending;
    m.status = 'streaming';
    const job = { chatId: id, connId: chat?.chat.connection_id, user: null, message: m, started: Date.now(),
      first: !!m.content, lost: false, controller: new AbortController() };
    jobs.set(id, job);
    notice.textContent = '';
    answering.textContent = tr("Модель отвечает…");
    renderFeed();
    syncBusy();
    kick();
    readServer(job, reply);
  }

  /* ответ, который читает вкладка: перерисовка не чаще RENDER_MS */
  function refresh(job, final) {
    if (job.chatId !== chatId || !chat) {
      if (final) loadList();
      return;
    }
    if (!parts.has(job.message)) renderFeed();
    if (!final) {
      if (paintTimer) return;
      paintTimer = setTimeout(() => {
        paintTimer = null;
        lastPaint = Date.now();
        paint(job.message);
        status.paint();
        scrollDown(false);
      }, Math.max(0, RENDER_MS - (Date.now() - lastPaint)));
      return;
    }
    clearTimeout(paintTimer);
    paintTimer = null;
    const ending = { done: tr("Ответ готов"), stopped: tr("Ответ остановлен"), error: tr("Ответ прерван ошибкой") };
    answering.textContent = job.lost ? '' : ending[job.message.status] || '';
    renderFeed();                                    // «Ответить заново» — у последнего
    syncBusy();
    scrollDown(false);
    loadList();
    // ответ кончился — диалог с сервера: источники, «Распознать заново» у вложений и сообщения, сделанные тем
    // временем на другом устройстве. Несохранённый ответ прямого подключения не перечитывать: его текста и «Ответ не
    // сохранился» сервер не знает — перечитанный показал бы «ответ идёт в другой вкладке» или пустую строку
    if (!unsaved.has(job.message.id)) reload();
  }

  /* события */
  // Escape меню (модель, строка списка) ловит само меню (menu.js): здесь — только выдвижная панель списка
  const onKey = (event) => {
    if (event.key !== 'Escape' || event.defaultPrevented || document.querySelector('.popup-menu:popover-open')) return;
    panel(false);
  };
  document.addEventListener('keydown', onKey);
  document.addEventListener('studio-me', paintWeb);     // профиль перечитан: ключ Exa могли задать или убрать
  /* вкладка снова на виду или в фокусе, связь вернулась: диалог могли продолжить на другом устройстве — перечитать;
     ход, который отправляет или читает сама вкладка, перечитывание не трогает */
  const resume = () => {
    if (!alive || document.visibilityState === 'hidden') { clearTimeout(poll); return; }
    if (needsReload || remoteStream() || otherTab() || (!sending && !jobs.has(chatId))) { failures = 0; reload(); }
  };
  window.addEventListener('online', resume);
  window.addEventListener('focus', resume);
  document.addEventListener('visibilitychange', resume);
  /* страница вернулась из bfcache: `pagehide` дописал очередь к черновику (chat-queue.js) — поле показывает его, иначе
     первое нажатие перезаписало бы черновик прежним текстом поля. Поле занято правкой сообщения или уходящим сообщением
     очереди — черновик вернётся в поле, когда они кончатся (`closeEdit`, `unqueueField`) */
  const restored = (event) => {
    if (!event.persisted || !alive || chatId === undefined || document.documentElement.classList.contains('shrouded')) return;
    if (editing) editing.previous = dialogDrafts.text(chatId);
    else if (queuing && !queuing.back) queuing.typed = dialogDrafts.text(chatId);
    else dialogDrafts.enter(chatId);
  };
  window.addEventListener('pageshow', restored);
  const tick = setInterval(() => {
    const job = jobs.get(chatId);
    if (job && (!job.first || job.thinking)) paint(job.message);   // счётчики «загружаю модель» / «жду ответа» и «Думает…»
    status.paint();                                // время стадии и «последнее событие N с назад»
  }, 1000);

  fresh.addEventListener('click', newChat);
  toggle.addEventListener('click', () => panel(!root.classList.contains('panel-open')));
  backdrop.addEventListener('click', () => panel(false));
  webToggle.addEventListener('click', switchWeb);
  thinkToggle.addEventListener('click', switchThink);
  talkToggle.addEventListener('click', switchTalk);
  scroller.addEventListener('scroll', () => {
    stick = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - STICK_PX;
  }, { passive: true });
  for (const type of ['wheel', 'touchstart', 'pointerdown', 'keydown']) {   // человек тронул ленту — она его
    scroller.addEventListener(type, () => { found = null; }, { passive: true });
  }
  input.addEventListener('input', () => {
    autosize();
    dropWaiting();
    ring.schedule();
    if (!editing) dialogDrafts.typed(chatId);
  });
  queueAdd.addEventListener('click', enqueue);
  enterSends(input, () => form.requestSubmit());
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    submit(false);
  });
  file.addEventListener('change', () => {
    const picked = [...file.files];
    file.value = '';
    addFiles(picked);
  });
  stop.addEventListener('click', async () => {
    const job = jobs.get(chatId);
    stop.disabled = true;
    try {
      if (job) job.controller.abort();             // прямое подключение: обрыв запроса и PUT stopped
      if (chatId && (!job || job.message.via !== 'browser')) {
        await api('POST', `/api/chats/${chatId}/stop`);
        if (!job) reload();
      }
    } catch (err) {
      notice.textContent = words(err);
    } finally {
      stop.disabled = false;
    }
  });

  const sectionsReady = api('GET', '/api/sections').then(({ sections }) => {
    const section = sections.find((s) => s.id === 'chat');
    links = section.links;
    chatDefault = section.default;
    paintChip();
  }, (err) => { notice.textContent = words(err); });
  const firstList = loadList();
  loadProjects();
  roles.reload().catch(() => {});
  kbs.refresh();                                   // нет баз — чипа нет
  api('GET', '/api/mcp').then(({ items }) => {     // нет списка — у нового диалога чип без числа
    mcpServers = items;
    paintWeb();
    // каталог — только если есть включённые серверы: из него `chat_files` (звук и видео в поле ввода)
    if (!items.some((s) => s.enabled)) return null;
    api('GET', '/api/mcp-tools/chat').then(({ items: sources }) => {
      if (!alive) return;
      mcpModelServers = sources.filter(server => server.models?.length);
      if (chatId === null && ctx.hint && mcpModelServers.length) {
        form.hidden = false;
        feed.replaceChildren(emptyNote());
      }
      paintChip();
    }, () => {});
    return api('GET', '/api/mcp/catalog').then((out) => {
      catalog = out.items;
      paintWeb();                                  // и звук с видео: веб-инструменты MCP открывают «Интернет» без ключа
      // состояние — только если его объявил хоть один сервер (`contracts.status`)
      return catalog.some((s) => s.contracts?.status) ? api('GET', '/api/mcp/status') : null;
    }).then((out) => {
      if (!out) return;
      mcpStatus = out;
      paintMcpState();
    });
  }).catch(() => {});

  return {
    open,
    refresh,
    query(query) {                                 // #/chat?transcript=<id>&ask=… или ?ocr=<id>&ask=…
      const tid = query && query.get('transcript');
      const sid = query && query.get('ocr');
      const mid = query && query.get('m');           // #/chat/<id>?m=<сообщение> — из поиска
      found = chatId && mid && /^[\w-]+$/.test(mid) ? { id: mid, lit: false } : null;
      if (found && chat) reveal();                   // диалог уже открыт; иначе — open() по приходе истории
      if (tid) fromTranscript(tid, query.get('ask') || 'discuss');
      else if (sid) fromScan(sid, query.get('ask') || 'discuss');
    },
    destroy() {
      if (talking) talking();
      closeEdit();
      unqueueField();
      if (chatId !== undefined) dialogDrafts.leave(chatId);   // черновик — у диалога (chat-drafts.js), не у раздела
      queue.unmount();
      nav.unmount();
      find.unmount();
      title.unmount();
      feedWatch.disconnect();
      clearTimeout(idleTimer);
      ring.hide();
      status.unmount();
      skills.unmount();
      inputDraft.save(false);
      sharing.unmount();
      selQuote.unmount();
      msgMenu.unmount();
      listing.unmount();
      tabs.unmount();
      alive = false;
      historyRead?.controller.abort();
      window.removeEventListener('online', resume);
      window.removeEventListener('focus', resume);
      window.removeEventListener('pageshow', restored);
      document.removeEventListener('visibilitychange', resume);
      document.removeEventListener('keydown', onKey);
      models.close();
      roles.close();
      effort.close();
      document.removeEventListener('studio-me', paintWeb);
      clearInterval(tick);
      clearTimeout(poll);
      stopWaiting();
      clearTimeout(paintTimer);
    },
  };
}

export function mount(view, ctx) {
  screen = build(view, ctx);
  const restore = !ctx.id && !ctx.query?.size && lastChat !== null;
  if (restore) history.replaceState(history.state, '', `#/chat/${encodeURIComponent(lastChat)}`);
  screen.open(restore ? lastChat : ctx.id, restore, ctx.query?.get('p'));
  screen.query(ctx.query);
}

export function update(ctx) {
  if (!screen) return;
  screen.open(ctx.id, false, ctx.query?.get('p'));
  screen.query(ctx.query);
}

export function unmount() {
  if (screen) screen.destroy();
  screen = null;
}
