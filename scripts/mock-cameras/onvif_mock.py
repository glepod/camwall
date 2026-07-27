#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os

PORT = int(os.environ.get("CAMWALL_MOCK_ONVIF_PORT", "2020"))

SOAP_OK = b"""<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
  <s:Body><Response xmlns="http://www.onvif.org/ver20/ptz/wsdl"/></s:Body>
</s:Envelope>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/soap+xml")
        self.send_header("Content-Length", str(len(SOAP_OK)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(SOAP_OK)

    def do_GET(self):
        self._ok()

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        if length:
            self.rfile.read(length)
        self._ok()

    do_HEAD = do_GET


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
