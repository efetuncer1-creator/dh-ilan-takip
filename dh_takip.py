"""
DonanımHaber ilan takip botu.

Konunun son sayfasını belirli aralıklarla kontrol eder. Yeni bir mesajda
aranan oyunlardan biri (OYUNLAR) ile hak türü ("pri") aynı ilanda geçerse
bildirim gönderir ve fiyatı söyler. Sayfa dolup yeni sayfa açıldığında otomatik
olarak yeni sayfaya geçer.

Kullanım:
    python dh_takip.py                 # sürekli takip
    python dh_takip.py --bir-kez       # tek tarama yap ve çık
    python dh_takip.py --oyun "Mortal Shell 2" --bir-kez   # farklı oyunla dene
"""

import argparse
import base64
import html
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

# ---------------- AYARLAR ----------------
BASLANGIC_URL = (
    "https://forum.donanimhaber.com/ps4-ps5-dijital-ortaklik-oyun-ve-hesap-satis-amp-takas-grubu-satilik-ilanlar--109819213-11444"
)
# Aranan oyunlar ve satıcıların farklı yazımları
# (küçük harf, Türkçe karakterler i'ye çevrilmiş halde aranır)
OYUNLAR = {
    "Phantom Blade Zero": [
        r"(?:ph|f)ant[o0]m\W*bl[a4]de?(?:\W*(?:zero|zer0|0|sifir))?",  # phantom blade (zero), fantom blade, phantom blad 0
        r"(?:ph|f)antomblade\w*",                                      # phantombladezero
        r"\bpbz\b",                                                    # PBZ kısaltması
        r"\bp\.\s*b\.\s*z\b",                                          # P.B.Z
    ],
    "Modern Warfare 4": [
        r"modern\W*warfare\W*(?:4(?!\d)|iv\b)",                        # modern warfare 4 / IV, cod: modern warfare 4
        r"modernwarfare\W*(?:4(?!\d)|iv\b)",                           # modernwarfare4
        r"\bmw\W*(?:4(?!\d)|iv\b)",                                    # mw4, MW 4, mw-4, MWIV
    ],
}
CIFT_HAK_DESENI = r"\b[çc]ift\W*hak"  # "Çift Hak", "cift hak", "çifthak" (pri + sec birlikte)
HAK_DESENI = r"\bpri(?:mary)?\b|" + CIFT_HAK_DESENI   # "pri", "Pri", "PRİ", "primary" veya çift hak
KONTROL_ARALIGI = 60                  # saniye
ILK_TARAMA_SAYFA = 3                  # ilk açılışta geriye doğru kaç sayfa taransın

# -----------------------------------------

KLASOR = Path(__file__).resolve().parent
DURUM_DOSYASI = KLASOR / "durum.json"
KAYIT_DOSYASI = KLASOR / "bulunanlar.txt"

# Telefona bildirim (ntfy) kanal adı: GitHub'da NTFY_KANAL secret'ından, bilgisayarda
# ntfy_kanal.txt dosyasından okunur. Kanal adı gizli kalsın diye koda yazılmaz.
_kanal_dosyasi = KLASOR / "ntfy_kanal.txt"
NTFY_KANAL = os.environ.get("NTFY_KANAL") or (
    _kanal_dosyasi.read_text(encoding="utf-8").strip() if _kanal_dosyasi.exists() else "")

OTURUM = requests.Session()
OTURUM.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9",
})


def log(mesaj):
    print(f"[{datetime.now():%H:%M:%S}] {mesaj}", flush=True)


def normalize(metin):
    # Türkçe büyük İ/I küçültmede sorun çıkarmasın
    return metin.replace("İ", "i").replace("I", "i").replace("ı", "i").lower()


def oyun_deseni(oyun):
    if oyun in OYUNLAR:
        return re.compile("|".join(f"(?:{d})" for d in OYUNLAR[oyun]))
    kelimeler = normalize(oyun).split()
    # "Phantom Blade Zero" -> phantom\W*blade\W*(zero|0)
    parcalar = [re.escape(k) if k != "zero" else "(?:zero|0)" for k in kelimeler]
    return re.compile(r"\W*".join(parcalar))


# ---------------- SAYFA OKUMA ----------------

def sayfa_getir(url):
    r = OTURUM.get(url, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def sayfa_bilgisi(sayfa_html):
    m = re.search(r'<section class="sayfacik"[^>]*>', sayfa_html)
    etiket = m.group(0) if m else ""
    sonraki = re.search(r'data-next-href="([^"]*)"', etiket)
    onceki = re.search(r'data-prev-href="([^"]*)"', etiket)
    no = re.search(r'data-page="(\d+)"', etiket)
    return {
        "sonraki": sonraki.group(1) if sonraki else "",
        "onceki": onceki.group(1) if onceki else "",
        "no": int(no.group(1)) if no else None,
    }


def html_to_metin(parca):
    parca = re.sub(r"(?is)<script.*?</script>", " ", parca)
    parca = re.sub(r"(?i)<br\s*/?>|</?(?:p|div|li|tr)\b[^>]*>", "\n", parca)
    parca = re.sub(r"<[^>]+>", " ", parca)
    parca = html.unescape(parca)
    satirlar = [re.sub(r"[ \t\xa0]+", " ", s).strip() for s in parca.split("\n")]
    metin = "\n".join(satirlar).strip()
    return re.sub(r"\n{3,}", "\n\n", metin)


def mesajlari_ayikla(sayfa_html):
    mesajlar = []
    parcalar = re.split(r'(?=<li id="mess_\d+")', sayfa_html)
    for p in parcalar[1:]:
        mid = re.match(r'<li id="mess_(\d+)"', p).group(1)
        kullanici = re.search(r'class="ki-kullaniciadi[^>]*>\s*<a[^>]*>\s*<b>(.*?)</b>', p, re.S)
        tarih = re.search(r'<time datetime="([^"]+)"', p)
        govde = re.search(r'<span class="msg">(.*?)(?:<span class="info">|<script|</section>)', p, re.S)
        if not govde:
            continue
        mesajlar.append({
            "id": mid,
            "kullanici": html.unescape(kullanici.group(1)).strip() if kullanici else "?",
            "tarih": tarih.group(1) if tarih else "",
            "metin": html_to_metin(govde.group(1)),
            "link": f"https://forum.donanimhaber.com/mesaj/yonlen/{mid}",
        })
    return mesajlar


# ---------------- EŞLEŞME VE FİYAT ----------------

def ilanlara_bol(metin):
    # Bir mesajda birden fazla ilan olabiliyor: "SATILIK" başlıklarından böl
    bloklar = re.split(r"(?im)^\s*(?=sat[iı]l[iı]k\b)", metin)
    bloklar = [b.strip() for b in bloklar if b.strip()]
    return bloklar or [metin]


def hak_metni(metin):
    # Şablondaki "Pri-Sec: Sec" etiketi "pri" sayılmasın, sadece değerine bakılsın
    return re.sub(r"\(?pri\w*[\s\-/|]*sec\w*\)?\s*[:=]", " ", normalize(metin))


def satildi_mi(blok):
    ilk_satirlar = " ".join(blok.split("\n")[:2])
    return re.search(r"\bsatildi\b", normalize(ilk_satirlar)) is not None


def fiyat_bul(blok, oyun_re, hak_re):
    satirlar = blok.split("\n")
    # 1) "Fiyat: 1.500₺" alanı
    for s in satirlar:
        m = re.search(r"(?i)fiyat\w*\s*[:=\-]?\s*(.+)", s)
        if m and re.search(r"\d", m.group(1)):
            return m.group(1).strip()
    # 2) hem pri hem rakam geçen satır ("pri 1500 sec 800")
    for s in satirlar:
        if hak_re.search(hak_metni(s)) and re.search(r"\d{2,}", s):
            return s.strip()
    # 3) oyunun geçtiği satırda rakam
    for s in satirlar:
        if oyun_re.search(normalize(s)) and re.search(r"\d{3,}\s*(?:₺|tl|lira)?", s, re.I):
            return s.strip()
    # 4) para birimli herhangi bir sayı
    m = re.search(r"\d[\d.,]*\s*(?:₺|tl\b|lira)", blok, re.I)
    if m:
        return m.group(0).strip()
    return "belirtilmemiş"


def eslesmeleri_bul(mesaj, oyun_re, hak_re):
    sonuc = []
    tum = mesaj["metin"]
    if not (oyun_re.search(normalize(tum)) and hak_re.search(hak_metni(tum))):
        return sonuc
    bloklar = ilanlara_bol(tum)
    for blok in bloklar:
        if oyun_re.search(normalize(blok)) and hak_re.search(hak_metni(blok)) and not satildi_mi(blok):
            sonuc.append({"blok": blok, "fiyat": fiyat_bul(blok, oyun_re, hak_re)})
    return sonuc


# ---------------- BİLDİRİM ----------------

def windows_bildirim(baslik, govde, link):
    if sys.platform != "win32":   # GitHub Actions'ta (Linux) sadece telefona gider
        return

    def x(s):
        return html.escape(s, quote=True)

    toast = (
        f'<toast activationType="protocol" launch="{x(link)}" scenario="reminder">'
        f'<visual><binding template="ToastGeneric"><text>{x(baslik)}</text>'
        f'<text>{x(govde)}</text></binding></visual>'
        f'<actions><action content="İlanı aç" activationType="protocol" arguments="{x(link)}"/>'
        f'<action content="Kapat" activationType="system" arguments="dismiss"/></actions>'
        f'<audio src="ms-winsoundevent:Notification.Looping.Alarm" loop="false"/></toast>'
    )
    ps = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml(@'
{toast}
'@)
$appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($xml))
"""
    kodlu = base64.b64encode(ps.encode("utf-16-le")).decode()
    try:
        subprocess.run(["powershell", "-NoProfile", "-EncodedCommand", kodlu],
                       check=True, capture_output=True, timeout=30)
    except Exception as e:
        log(f"Windows bildirimi gösterilemedi: {e}")


def telefon_bildirim(baslik, metin, link):
    if not NTFY_KANAL:
        return
    try:
        r = requests.post("https://ntfy.sh/", timeout=15, json={
            "topic": NTFY_KANAL,
            "title": baslik,
            "message": metin,
            "click": link,
            "priority": 5,
            "tags": ["video_game"],
            "actions": [{"action": "view", "label": "İlanı aç", "url": link}],
        })
        r.raise_for_status()
    except Exception as e:
        log(f"Telefon bildirimi gönderilemedi: {e}")


def haber_ver(oyun, mesaj, eslesme):
    fiyat = eslesme["fiyat"]
    hak_metin = hak_metni(eslesme["blok"])
    hak = "pri" if re.search(r"\bpri(?:mary)?\b", hak_metin) else "çift hak"
    baslik = f"{oyun} ({hak}) — Fiyat: {fiyat}"
    govde = f"{mesaj['kullanici']}: " + " | ".join(
        s for s in eslesme["blok"].split("\n") if s.strip())[:200]
    log("=" * 60)
    log(f"BULUNDU! {baslik}")
    log(f"Satıcı: {mesaj['kullanici']}  Tarih: {mesaj['tarih']}")
    log(f"Link: {mesaj['link']}")
    for s in eslesme["blok"].split("\n"):
        if s.strip():
            log(f"   {s}")
    log("=" * 60)
    with open(KAYIT_DOSYASI, "a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {fiyat} | {mesaj['kullanici']} | {mesaj['link']}\n"
                f"{eslesme['blok']}\n{'-' * 60}\n")
    windows_bildirim(baslik, govde, mesaj["link"])
    telefon_bildirim(baslik, f"Satıcı: {mesaj['kullanici']}\n{eslesme['blok']}", mesaj["link"])


# ---------------- DURUM ----------------

def durum_yukle():
    if DURUM_DOSYASI.exists():
        try:
            return json.loads(DURUM_DOSYASI.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"son_url": BASLANGIC_URL, "gorulen": [], "ilk_tarama_yapildi": False}


def durum_kaydet(durum):
    durum["gorulen"] = durum["gorulen"][-3000:]
    DURUM_DOSYASI.write_text(json.dumps(durum, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------- ANA DÖNGÜ ----------------

def ilan_basligi(mesaj, oyunlar, hak_re):
    oyun = re.search(r"(?im)^\s*(?:oyun\s*ad[iı]|oyun)\s*[:=]\s*(.+)$", mesaj["metin"])
    oyun = oyun.group(1).strip() if oyun else "İlan"
    tum_oyunlar = re.compile("|".join(o.pattern for o in oyunlar.values()))
    return f"{oyun} — Fiyat: {fiyat_bul(mesaj['metin'], tum_oyunlar, hak_re)}"


def sayfayi_isle(url, durum, gorulen, oyunlar, hak_re, hepsi=False):
    sayfa_html = sayfa_getir(url)
    bilgi = sayfa_bilgisi(sayfa_html)
    yeni = 0
    for mesaj in mesajlari_ayikla(sayfa_html):
        if mesaj["id"] in gorulen:
            continue
        gorulen.add(mesaj["id"])
        durum["gorulen"].append(mesaj["id"])
        yeni += 1
        bulundu = False
        for oyun, oyun_re in oyunlar.items():
            for e in eslesmeleri_bul(mesaj, oyun_re, hak_re):
                haber_ver(oyun, mesaj, e)
                bulundu = True
        if hepsi and not bulundu:
            # Test modu: eşleşmeyen her yeni mesaj da telefona gelsin
            baslik = "[TEST] " + ilan_basligi(mesaj, oyunlar, hak_re)
            log(f"Test bildirimi: {baslik}")
            telefon_bildirim(baslik, f"Satıcı: {mesaj['kullanici']}\n{mesaj['metin']}", mesaj["link"])
    return bilgi, yeni


def tara(durum, oyunlar, hak_re, hepsi=False):
    gorulen = set(durum["gorulen"])

    if not durum.get("ilk_tarama_yapildi"):
        # Son sayfadan geriye doğru birkaç sayfayı da tara
        url = durum["son_url"]
        geri = []
        for _ in range(ILK_TARAMA_SAYFA - 1):
            b = sayfa_bilgisi(sayfa_getir(url))
            if not b["onceki"]:
                break
            url = b["onceki"]
            geri.append(url)
        for u in reversed(geri):
            sayfayi_isle(u, durum, gorulen, oyunlar, hak_re)
        durum["ilk_tarama_yapildi"] = True
        log(f"İlk tarama: son {len(geri) + 1} sayfa kontrol edildi.")

    url = durum["son_url"]
    while True:
        bilgi, yeni = sayfayi_isle(url, durum, gorulen, oyunlar, hak_re, hepsi)
        if bilgi["sonraki"]:
            url = bilgi["sonraki"]
            if not url.startswith("http"):
                url = "https://forum.donanimhaber.com" + url
            durum["son_url"] = url
            log(f"Yeni sayfa açılmış, geçiliyor: sayfa {re.search(r'-(\d+)$', url).group(1)}")
            continue
        log(f"Sayfa {bilgi['no']} kontrol edildi, {yeni} yeni mesaj.")
        break
    durum_kaydet(durum)


def son_ilani_goster(url):
    # Gerçekten son sayfaya kadar ilerle, oradaki son mesajı bildir
    while True:
        sayfa_html = sayfa_getir(url)
        sonraki = sayfa_bilgisi(sayfa_html)["sonraki"]
        if not sonraki:
            break
        url = sonraki if sonraki.startswith("http") else "https://forum.donanimhaber.com" + sonraki
    mesaj = mesajlari_ayikla(sayfa_html)[-1]
    baslik = ilan_basligi(mesaj, {k: oyun_deseni(k) for k in OYUNLAR}, re.compile(HAK_DESENI))
    log(f"Son ilan ({mesaj['tarih']}): {baslik}")
    log(f"Link: {mesaj['link']}")
    for s in mesaj["metin"].split("\n"):
        if s.strip():
            log(f"   {s}")
    telefon_bildirim(baslik, f"Satıcı: {mesaj['kullanici']}\n{mesaj['metin']}", mesaj["link"])


def main():
    ap = argparse.ArgumentParser(description="DonanımHaber ilan takip botu")
    ap.add_argument("--oyun", help="listedekiler yerine sadece bu oyunu ara")
    ap.add_argument("--aralik", type=int, default=KONTROL_ARALIGI, help="kontrol aralığı (sn)")
    ap.add_argument("--bir-kez", action="store_true", help="tek tarama yap ve çık")
    ap.add_argument("--sifirla", action="store_true", help="kayıtlı durumu sil, baştan tara")
    ap.add_argument("--bildirim-test", action="store_true", help="sadece deneme bildirimi göster")
    ap.add_argument("--son-ilan", action="store_true", help="konudaki en son mesajı bildirim olarak göster")
    ap.add_argument("--hepsi", action="store_true",
                    help="test modu: eşleşmeyen yeni mesajları da telefona gönder (TEST_MODU=1 ile de açılır)")
    args = ap.parse_args()
    hepsi = args.hepsi or os.environ.get("TEST_MODU", "").strip() in ("1", "true", "evet")

    if args.bildirim_test:
        telefon_bildirim("DH Takip — deneme", "Telefon bildirimleri çalışıyor 👍", BASLANGIC_URL)
        return

    if args.son_ilan:
        son_ilani_goster(durum_yukle()["son_url"])
        return

    if args.sifirla and DURUM_DOSYASI.exists():
        DURUM_DOSYASI.unlink()

    isimler = [args.oyun] if args.oyun else list(OYUNLAR)
    oyunlar = {isim: oyun_deseni(isim) for isim in isimler}
    hak_re = re.compile(HAK_DESENI)
    durum = durum_yukle()
    log(f"Takip başladı: {', '.join(isimler)} + pri/çift hak | her {args.aralik} sn"
        + (" | TEST MODU: tüm yeni mesajlar gönderilecek" if hepsi else ""))

    while True:
        hata = False
        try:
            tara(durum, oyunlar, hak_re, hepsi)
        except requests.RequestException as e:
            log(f"Bağlantı hatası: {e}")
            hata = True
        except Exception as e:
            log(f"Hata: {e!r}")
            hata = True
        if args.bir_kez:
            # GitHub Actions'ta hata kırmızı görünsün ve e-posta gelsin
            sys.exit(1 if hata else 0)
        time.sleep(args.aralik + random.uniform(0, 10))


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    try:
        main()
    except KeyboardInterrupt:
        log("Durduruldu.")
