"""A formatting-only HTML allowlist for an owner's selected chat fragment."""
import html
from html.parser import HTMLParser
import re

from fastapi import HTTPException


TAGS = set('div span p br hr h1 h2 h3 h4 h5 h6 strong b em i del s u code pre blockquote '
           'ul ol li table thead tbody tfoot tr th td a sub sup'.split())
VOID = {'br', 'hr'}
DROP = {'script', 'style', 'iframe', 'object', 'svg', 'math', 'template', 'button', 'form'}
CLASSES = re.compile(r'(?:hljs(?:-[\w-]+)?|language-[\w+#-]+|class_|function_|language_|chat-export-plain)\Z')

# Print on a light page, with the same hierarchy, spacing and syntax colours as chat Markdown.
# No user CSS, fonts, images or other resources reach the converter.
CSS = """
.chat-export { font-family: Arial, sans-serif; font-size: 11pt; line-height: 1.5; color: #202124; overflow-wrap: anywhere; }
.chat-export p, .chat-export ul, .chat-export ol, .chat-export blockquote, .chat-export table, .chat-export pre { margin: 0 0 .75em; }
.chat-export h1, .chat-export h2, .chat-export h3, .chat-export h4 { margin: 1em 0 .5em; border: 0; padding: 0; }
.chat-export h1 { font-size: 1.35em; } .chat-export h2 { font-size: 1.2em; }
.chat-export h3, .chat-export h4 { font-size: 1.05em; }
.chat-export ul, .chat-export ol { padding-left: 1.4em; } .chat-export li { margin: .2em 0; }
.chat-export blockquote { padding-left: 12px; border-left: 3px solid #b8bcc3; color: #555b65; }
.chat-export a { color: inherit; text-decoration: underline; }
.chat-export code { font-family: Consolas, 'Courier New', monospace; font-size: .9em; background: #f1f3f5; }
.chat-export pre { padding: 12px 14px; background: #f1f3f5; white-space: pre-wrap; overflow-wrap: anywhere; }
.chat-export pre code { padding: 0; background: none; font-size: 10pt; }
.chat-export table { border-collapse: collapse; width: 100%; }
.chat-export th, .chat-export td { border: 1px solid #b8bcc3; padding: 6px 10px; }
.chat-export th { background: #f1f3f5; }
.chat-export [align="left"] { text-align: left; } .chat-export [align="center"] { text-align: center; }
.chat-export [align="right"] { text-align: right; }
.chat-export-plain { white-space: pre-wrap; margin-bottom: .75em; }
.hljs-keyword, .hljs-doctag, .hljs-type, .hljs-template-tag { color: #a32756; }
.hljs-title { color: #7040a0; } .hljs-string, .hljs-regexp { color: #126334; }
.hljs-number, .hljs-literal, .hljs-attr, .hljs-attribute, .hljs-variable, .hljs-meta, .hljs-operator { color: #7e460d; }
.hljs-built_in, .hljs-symbol { color: #775600; } .hljs-comment, .hljs-quote { color: #626a73; }
.hljs-emphasis { font-style: italic; } .hljs-strong { font-weight: bold; }
"""


class SelectionHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.stack = []
        self.blocked = []
        self.has_text = False
        self.nodes = 0

    def handle_starttag(self, tag, attrs):
        self.nodes += 1
        if self.nodes > 20000 or len(self.stack) + len(self.blocked) > 128:
            raise HTTPException(413, {'code': 'selection_large', 'message': 'выделите меньший фрагмент'})
        if tag in DROP:
            self.blocked.append(tag)
        if self.blocked or tag not in TAGS:
            return
        safe = {}
        for key, value in attrs:
            if value is None:
                continue
            if key == 'href' and tag == 'a' and re.match(r'^(?:https?://|mailto:)', value, re.I):
                safe[key] = value
            elif key in {'title', 'alt'}:
                safe[key] = value
            elif key == 'class':
                safe[key] = ' '.join(c for c in value.split() if CLASSES.fullmatch(c))
            elif key == 'align' and tag in {'th', 'td', 'p', 'div'} and value in {'left', 'center', 'right'}:
                safe[key] = value
            elif ((key == 'start' and tag == 'ol') or (key == 'value' and tag == 'li')
                  or (key in {'colspan', 'rowspan'} and tag in {'th', 'td'})) and re.fullmatch(r'-?\d{1,6}', value):
                safe[key] = value
        self.parts.append('<' + tag + ''.join(f' {key}="{html.escape(value, quote=True)}"'
                                             for key, value in safe.items()) + '>')
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.blocked:
            if tag == self.blocked[-1]:
                self.blocked.pop()
            return
        if tag in self.stack:
            while self.stack:
                current = self.stack.pop()
                self.parts.append('</' + current + '>')
                if current == tag:
                    break

    def handle_data(self, data):
        if not self.blocked:
            self.has_text |= bool(data.strip())
            # Keep blank lines in pre-wrap user messages through md2pdf's second Markdown pass.
            self.parts.append(html.escape(data).replace('\n', '&#10;'))


def selection_html(fragment: str) -> str:
    parser = SelectionHTML()
    parser.feed(fragment)
    parser.close()
    if not parser.has_text:
        raise HTTPException(422, {'code': 'selection_empty', 'message': 'сначала выделите текст в чате'})
    return '<style>' + CSS + '</style><div class="chat-export">' + ''.join(parser.parts) + ''.join(
        '</' + tag + '>' for tag in reversed(parser.stack)) + '</div>'
