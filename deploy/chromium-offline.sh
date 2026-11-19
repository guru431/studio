#!/bin/sh
# Chromium for the server's PDF export: chat_exports.render_pdf hands it to md2pdf as MD2PDF_BROWSER.
# The web container runs with network_mode: host, so a stray image URL in the printed page would be
# fetched from the Docker host past the allowed-address list. Here the browser has no network:
#  - every http(s) request goes to a proxy that cannot be reached, with no DIRECT fallback;
#    <-loopback> removes the implicit bypass for localhost, 127.0.0.1 and [::1];
#  - every host name, IP literals included, fails to resolve.
# Measured 2026-11-19 with Edge and Chrome on an IP literal, loopback, LAN address, DNS name and CSS
# url(): no request reached the server; the proxy without <-loopback> let loopback through.
exec chromium \
    --proxy-server=socks5://255.255.255.255:9 \
    '--proxy-bypass-list=<-loopback>' \
    '--host-resolver-rules=MAP * ~NOTFOUND' \
    "$@"
