"""Der eigene Server fürs Zusammenspielen (Weißlochhaus).

Ein einziges Programm, nur mit dem, was Python schon mitbringt (nichts nachzuinstallieren):
  1. Es liefert das Spiel aus (die Dateien der Web-Fassung), so wie sonst GitHub Pages.
  2. Es verbindet die Spieler: Unter /netz nimmt es WebSocket-Verbindungen an. Einer eröffnet einen Raum und bekommt
     einen Code aus vier Buchstaben, die anderen treten mit dem Code bei. Der Server reicht dann jede Textzeile weiter
     (vom Gastgeber an alle Gäste, von einem Gast an den Gastgeber und die anderen Gäste).

Die Webseite merkt selbst, dass sie von diesem Server kommt (sie fragt /netz-info ab), und nimmt dann diesen Weg
statt des fremden Dienstes PeerJS. Die Geräte müssen sich dabei nicht gegenseitig finden – jedes redet nur mit dem Server.

Aufruf:   python netz_server.py [--ordner PFAD] [--port 8070]
          Ohne --ordner sucht es die Web-Fassung neben sich: ../unity (im Ordner von GitHub Pages) oder ../build/unity-web.
Von außen erreichbar wird der PC z. B. mit Tailscale Funnel:   tailscale funnel 8070

Zeilen zwischen Seite und Server (alles Text):
  Seite -> Server, als Erstes:  "H" (Raum eröffnen)  oder  "J|CODE" (beitreten)
  Server -> Gastgeber:          "C|CODE"   dann  "+|Platz" (Gast da)  und  "-|Platz" (Gast weg)
  Server -> Gast:               "W|Platz"  oder  "F|Grund" (kein Raum / voll);  "X|Platz" (anderer Gast weg);  "Z" (Gastgeber weg)
  Alles andere wird unverändert weitergereicht.
"""
import argparse
import asyncio
import base64
import hashlib
import mimetypes
import pathlib
import secrets
import struct
import sys
import time

HIER = pathlib.Path(__file__).resolve().parent
BUCHSTABEN = "ABCDEFGHJKLMNPQRSTUVWXYZ"
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
HOECHSTENS = 4096           # so lang darf eine Zeile höchstens sein (Bytes)
ARTEN = {".wasm": "application/wasm", ".js": "text/javascript", ".data": "application/octet-stream", ".json": "application/json",
         ".webmanifest": "application/manifest+json", ".html": "text/html; charset=utf-8", ".mp4": "video/mp4", ".ttf": "font/ttf",
         ".png": "image/png", ".unityweb": "application/octet-stream"}

raeume = {}                 # Code -> Raum


class Raum:
    def __init__(self, code, wirt):
        self.code = code
        self.wirt = wirt
        self.gaeste = {}    # Platz 1..3 -> Leitung


class Leitung:
    """Eine WebSocket-Verbindung zu einem Spieler."""

    def __init__(self, lesen, schreiben):
        self.lesen, self.schreiben = lesen, schreiben
        self.raum = None
        self.platz = -1
        self.zu = False

    async def sende(self, text):
        if self.zu:
            return
        daten = text.encode("utf-8")
        n = len(daten)
        kopf = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n))
        try:
            self.schreiben.write(kopf + daten)
            await self.schreiben.drain()
        except Exception:
            self.zu = True

    async def lies(self):
        """Die nächste Textzeile. None = Verbindung ist zu."""
        while True:
            kopf = await self.lesen.readexactly(2)
            art, lang = kopf[0] & 0x0F, kopf[1] & 0x7F
            maske = kopf[1] & 0x80
            if lang == 126:
                lang = struct.unpack(">H", await self.lesen.readexactly(2))[0]
            elif lang == 127:
                lang = struct.unpack(">Q", await self.lesen.readexactly(8))[0]
            if lang > HOECHSTENS:
                return None
            schluessel = await self.lesen.readexactly(4) if maske else b"\0\0\0\0"
            daten = bytearray(await self.lesen.readexactly(lang))
            for i in range(lang):
                daten[i] ^= schluessel[i & 3]
            if art == 8:                                   # Schließen
                return None
            if art == 9:                                   # Ping -> Pong
                self.schreiben.write(bytes([0x8A, len(daten)]) + bytes(daten))
                continue
            if art == 1:
                return daten.decode("utf-8", "replace")
            # alles andere (Pong, Binär, Fortsetzung) wird übergangen

    def schliesse(self):
        self.zu = True
        try:
            self.schreiben.close()
        except Exception:
            pass


def melde(text):
    print(time.strftime("%H:%M:%S"), text, flush=True)


async def netz(leitung):
    """Ein Spieler: erst eröffnen oder beitreten, dann Zeilen weiterreichen, am Ende aufräumen."""
    try:
        erste = await asyncio.wait_for(leitung.lies(), 20)
        if erste is None:
            return
        if erste == "H":
            for _ in range(50):
                code = "".join(secrets.choice(BUCHSTABEN) for _ in range(4))
                if code not in raeume:
                    break
            else:
                await leitung.sende("F|Kein freier Code")
                return
            raum = raeume[code] = Raum(code, leitung)
            leitung.raum, leitung.platz = raum, 0
            await leitung.sende("C|" + code)
            melde(f"Raum {code} eröffnet ({len(raeume)} offen)")
        elif erste.startswith("J|"):
            raum = raeume.get(erste[2:].strip().upper())
            if raum is None:
                await leitung.sende("F|Kein Spiel mit diesem Code gefunden.")
                return
            platz = next((p for p in (1, 2, 3) if p not in raum.gaeste), 0)
            if not platz:
                await leitung.sende("F|Dieses Spiel ist schon voll (vier Spieler).")
                return
            raum.gaeste[platz] = leitung
            leitung.raum, leitung.platz = raum, platz
            await leitung.sende("W|" + str(platz))
            await raum.wirt.sende("+|" + str(platz))
            melde(f"Raum {raum.code}: Platz {platz} besetzt")
        else:
            return

        raum = leitung.raum
        while True:
            zeile = await leitung.lies()
            if zeile is None:
                break
            if leitung.platz == 0:
                ziele = list(raum.gaeste.values())
            else:
                ziele = [raum.wirt] + [g for p, g in raum.gaeste.items() if p != leitung.platz]
            for z in ziele:
                await z.sende(zeile)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError):
        pass
    finally:
        raum = leitung.raum
        if raum is not None:
            if leitung.platz == 0:
                raeume.pop(raum.code, None)
                for g in list(raum.gaeste.values()):
                    await g.sende("Z")
                    g.schliesse()
                melde(f"Raum {raum.code} geschlossen")
            elif raum.gaeste.get(leitung.platz) is leitung:
                del raum.gaeste[leitung.platz]
                await raum.wirt.sende("-|" + str(leitung.platz))
                for g in list(raum.gaeste.values()):
                    await g.sende("X|" + str(leitung.platz))
                melde(f"Raum {raum.code}: Platz {leitung.platz} frei")
        leitung.schliesse()


async def herzschlag():
    """Alle 20 Sekunden ein Ping an alle: So bleibt die Leitung auch durch Tunnel und Router offen."""
    while True:
        await asyncio.sleep(20)
        for raum in list(raeume.values()):
            for l in [raum.wirt] + list(raum.gaeste.values()):
                if not l.zu:
                    try:
                        l.schreiben.write(bytes([0x89, 0]))
                    except Exception:
                        l.zu = True


def antwort(status, kopf=None, inhalt=b""):
    zeilen = [f"HTTP/1.1 {status}", f"Content-Length: {len(inhalt)}", "Connection: close"]
    for k, v in (kopf or {}).items():
        zeilen.append(f"{k}: {v}")
    return ("\r\n".join(zeilen) + "\r\n\r\n").encode("ascii") + inhalt


async def bediene(lesen, schreiben, ordner):
    try:
        anfang = await asyncio.wait_for(lesen.readuntil(b"\r\n\r\n"), 15)
    except Exception:
        schreiben.close()
        return
    zeilen = anfang.decode("latin-1").split("\r\n")
    teile = zeilen[0].split(" ")
    kopf = {}
    for z in zeilen[1:]:
        if ":" in z:
            k, v = z.split(":", 1)
            kopf[k.strip().lower()] = v.strip()
    if len(teile) < 2 or teile[0] not in ("GET", "HEAD"):
        schreiben.write(antwort("405 Method Not Allowed"))
        schreiben.close()
        return
    pfad = teile[1].split("?")[0].split("#")[0]

    # ---- Mitspieler-Leitung
    if pfad.rstrip("/").endswith("/netz") or pfad == "/netz":
        schluessel = kopf.get("sec-websocket-key")
        if kopf.get("upgrade", "").lower() != "websocket" or not schluessel:
            schreiben.write(antwort("400 Bad Request"))
            schreiben.close()
            return
        zusage = base64.b64encode(hashlib.sha1((schluessel + GUID).encode("ascii")).digest()).decode("ascii")
        schreiben.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                         f"Sec-WebSocket-Accept: {zusage}\r\n\r\n").encode("ascii"))
        await netz(Leitung(lesen, schreiben))
        return
    if pfad.rstrip("/").endswith("/netz-info"):
        schreiben.write(antwort("200 OK", {"Content-Type": "application/json", "Cache-Control": "no-store"}, b'{"weisslochhaus":1}'))
        await schreiben.drain()
        schreiben.close()
        return

    # ---- Dateien des Spiels
    try:
        from urllib.parse import unquote
        rel = unquote(pfad).lstrip("/")
        datei = (ordner / rel).resolve()
        if datei.is_dir():
            datei = datei / "index.html"
        if ordner not in datei.parents and datei != ordner or not datei.is_file():
            schreiben.write(antwort("404 Not Found", {"Content-Type": "text/plain; charset=utf-8"}, "Nicht gefunden".encode("utf-8")))
            await schreiben.drain()
            schreiben.close()
            return
        groesse = datei.stat().st_size
        art = ARTEN.get(datei.suffix.lower()) or mimetypes.guess_type(datei.name)[0] or "application/octet-stream"
        frisch = datei.suffix.lower() in (".html", ".json", ".webmanifest")
        zusatz = {"Content-Type": art, "Accept-Ranges": "bytes", "Cache-Control": "no-cache" if frisch else "public, max-age=3600"}
        von, bis, status = 0, groesse - 1, "200 OK"
        bereich = kopf.get("range", "")
        if bereich.startswith("bytes=") and groesse > 0:          # iPhones holen Videos nur stückweise
            a, _, b = bereich[6:].split(",")[0].partition("-")
            try:
                if a == "":
                    von = max(0, groesse - int(b))
                else:
                    von = int(a)
                    bis = min(groesse - 1, int(b)) if b else groesse - 1
                if von > bis or von >= groesse:
                    raise ValueError
                status = "206 Partial Content"
                zusatz["Content-Range"] = f"bytes {von}-{bis}/{groesse}"
            except ValueError:
                von, bis, status = 0, groesse - 1, "200 OK"
        lang = bis - von + 1 if groesse > 0 else 0
        kopfzeilen = [f"HTTP/1.1 {status}", f"Content-Length: {lang}", "Connection: close"] + [f"{k}: {v}" for k, v in zusatz.items()]
        schreiben.write(("\r\n".join(kopfzeilen) + "\r\n\r\n").encode("ascii"))
        if teile[0] == "GET" and lang > 0:
            with open(datei, "rb") as f:
                f.seek(von)
                rest = lang
                while rest > 0:
                    stueck = f.read(min(262144, rest))
                    if not stueck:
                        break
                    schreiben.write(stueck)
                    await schreiben.drain()
                    rest -= len(stueck)
        await schreiben.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            schreiben.close()
        except Exception:
            pass


async def haupt():
    p = argparse.ArgumentParser(description="Server fürs Zusammenspielen (Weißlochhaus)")
    p.add_argument("--ordner", default="")
    p.add_argument("--port", type=int, default=8070)
    a = p.parse_args()
    kandidaten = [pathlib.Path(a.ordner)] if a.ordner else [HIER.parent / "unity", HIER.parent / "build" / "unity-web"]
    ordner = next((k.resolve() for k in kandidaten if (k / "index.html").is_file()), None)
    if ordner is None:
        sys.exit("Die Web-Fassung des Spiels wurde nicht gefunden (index.html fehlt). Mit --ordner den Pfad angeben.")
    server = await asyncio.start_server(lambda l, s: bediene(l, s, ordner), "0.0.0.0", a.port)
    asyncio.ensure_future(herzschlag())
    melde(f"Weißlochhaus-Server läuft. Spiel aus: {ordner}")
    melde(f"Auf diesem PC:  http://localhost:{a.port}/")
    melde("Von außen mit Tailscale Funnel:  tailscale funnel " + str(a.port))
    melde("Beenden: dieses Fenster schließen oder Strg+C.")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(haupt())
    except KeyboardInterrupt:
        pass
