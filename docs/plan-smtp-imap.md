# Plan: SMTP gönderim + IMAP okuma desteği

Amaç: Gmail API/OAuth dışında, herhangi bir mailbox'ı (Outlook/M365, Zoho, Yandex, kendi sunucu,
Gmail app password) **kendi SMTP/IMAP sunucusu** üzerinden bağlayabilmek. Gönderim yine kullanıcının
gerçek inbox'ından yapılır (ESP yok — bkz. "Neden SES değil").

İlke: tekerleği yeniden icat etmiyoruz. Protokol, bounce ayrıştırma, sağlayıcı ayarları ve test
sunucusu için olgun açık kaynak parçalar kullanılıyor; bizim yazdığımız kod sadece bunları mevcut
akışa (claim → send → record, reply/bounce → stop) bağlayan ince katman.

---

## 1. Hazır çözümler — ne kullanıyoruz, ne kullanmıyoruz

| İhtiyaç | Seçim | Neden | Durum (Kasım 2026) |
|---|---|---|---|
| SMTP gönderim | **`smtplib` (stdlib)** | Senkron worker'a birebir uyuyor, bağımlılık yok. `aiosmtplib`'e gerek yok (async değiliz). | stdlib |
| IMAP okuma | **[`imap-tools`](https://github.com/ikvk/imap_tools)** | Yüksek seviyeli API: UID ile arama/fetch, header parse, `MailMessage` nesnesi, IDLE, XOAUTH2 login. Kendi bağımlılığı yok. | 1.16.0, 2026‑10, Apache‑2.0 |
| Bounce ayrıştırma | **[`flufl.bounce`](https://pypi.org/project/flufl.bounce/)** | GNU Mailman'in bounce motoru: RFC 3464 DSN + onlarca sağlayıcıya özel format. "Hangi adres bounce etti" + kalıcı/geçici ayrımı veriyor. Bizim `BOUNCE_SENDERS` heuristic'imizin yerini alır (Gmail yolunda da kullanılabilir). | 6.0.0, 2026‑10, Apache‑2.0 |
| Sağlayıcı ayarları (host/port/TLS) | **[Thunderbird ISPDB / autoconfig](https://wiki.mozilla.org/Thunderbird:Autoconfiguration)** | Kullanıcı sadece e‑posta girer; `https://autoconfig.thunderbird.net/v1.1/<domain>` (+ domain'in kendi `autoconfig.<domain>` ve MX tabanlı lookup) host/port/socketType döner. Mevcut `httpx` ile çağrılır, yeni bağımlılık yok. | Mozilla bakımında |
| Reply'ın kendi metni (quote ayıklama) | **Mevcut `inbox.own_text`**, gerekirse **[`mail-parser-reply`](https://github.com/alfonsrv/mail-parser-reply)** | Bizim `own_text` zaten "kendi adresimizden itibaren kes" taktiğiyle Türkçe dahil her dilde çalışıyor. Yetmezse çok dilli `mail-parser-reply` (MIT) eklenir. **Faz 1'de eklenmiyor.** | 1.36, MIT |
| Testler | **[`aiosmtpd`](https://aiosmtpd.aio-libs.org/)** (SMTP, in‑process) + **[GreenMail](https://greenmail-mail-test.github.io/greenmail/)** (IMAP+SMTP, Docker, opsiyonel entegrasyon testi) | Sahte sunucu yazmıyoruz. Unit testler `aiosmtpd` Controller ile; IMAP için `imap-tools` katmanı fake'lenir, gerçek protokol GreenMail ile `-m integration` altında test edilir. | aiosmtpd 1.4.6 (dev dep) |

**Referans alınacak açık kaynak projeler** (kod kopyalamak için değil, sağlayıcı tuzaklarını ve
UX'i görmek için; lisanslarını kopyalamadan önce kontrol et):
[Warmbly](https://www.opensourcealternatives.to/item/warmbly) (Apache‑2.0, SMTP/IMAP, unified inbox),
[SubsiMail](https://www.producthunt.com/products/subsimail),
[Delerious28/SMTP](https://github.com/Delerious28/SMTP) (Python/FastAPI, SMTP/IMAP + OAuth).

**Bilerek kullanmadıklarımız:** SES/SendGrid/Mailgun (cold outreach'i ToS ile yasaklıyor, inbox'ı yok),
`talon` (bakımsız, Python 3.11+ sorunlu), RustMailer gibi ayrı servis olarak koşan middleware'ler
(tek süreçli, SQLite'lı basit mimarimize ağır).

---

## 2. Önemli dış kısıt: Microsoft 365

Exchange Online, SMTP AUTH için **Basic Auth'u (kullanıcı adı + şifre) Aralık 2026 sonunda varsayılan
olarak kapatıyor** ([MC786329](https://mc.merill.net/message/MC786329)). Yani M365 hesapları için
şifreyle SMTP birkaç hafta içinde çalışmayacak; **XOAUTH2** gerekecek.

Sonuç: auth katmanını baştan **takılabilir** tasarlıyoruz — `password` ve `xoauth2`. Faz 1'de
`password` (Zoho, Yandex, kendi sunucu, Gmail app password), M365 OAuth Faz 3'te. `smtplib.auth()`
ve `imap-tools` `xoauth2()` ikisi de XOAUTH2'yi destekliyor, yani sadece token alma akışı (MSAL
yerine mevcut `google.py` deseniyle elle `httpx`) yazılacak.

---

## 3. Mimari değişiklik

Bugün `sender.process` ve `inbox.poll/check_thread` doğrudan `Google` nesnesini çağırıyor. Araya
**mailbox başına bir `Transport`** koyuyoruz:

```
outreach_wise/transports/
    __init__.py      # for_account(account, cfg, google) -> Transport
    base.py          # Transport protokolü + ortak hata tipleri
    gmail.py         # mevcut google.py çağrılarını saran ince adapter (davranış değişmez)
    smtp_imap.py     # smtplib + imap-tools
autoconfig.py        # ISPDB lookup
```

```python
class Transport(Protocol):
    def send(self, mime: EmailMessage, reply_to: SentRef | None) -> SentRef: ...
    def fetch_new(self, since: datetime) -> list[Inbound]: ...  # poll
    def thread_replies(self, ref: SentRef) -> list[Inbound]: ...  # check_thread
    def verify(self) -> None: ...  # "Bağlantıyı test et"
```

- `SentRef`: `{provider_id, thread_key, rfc_message_id}`. Gmail'de `thread_key = threadId`,
  SMTP'de `thread_key = ilk mailin rfc_message_id'si`.
- Hata tipleri `google.py`'den `transports/base.py`'ye taşınır ve genelleşir:
  `AuthError`, `TransportError(retryable)`, `SendStateUnknown`. `sender.py` sadece bunları bilir.
- `build_mime` artık `EmailMessage` döner; base64 kodlamayı Gmail adapter'ı yapar.

### SMTP hata eşlemesi (duplicate korumasının kalbi)

| Durum | Sonuç |
|---|---|
| `SMTPAuthenticationError` (535) | `AuthError` → inbox duraklatılır |
| Bağlanırken/EHLO/AUTH sırasında kopma, 4xx | `TransportError(retryable=True)` |
| `SMTPRecipientsRefused`, 5xx | `TransportError(retryable=False)` → sequence durur |
| DATA gönderildikten **sonra** kopma/timeout | `SendStateUnknown` → tekrar denenmez |

Not: `smtplib.send_message` DATA'nın ne zaman bittiğini ayırmıyor; bu yüzden gönderimi
`mail` / `rcpt` / `data` adımlarıyla açıkça yazıyoruz (~40 satır).

### Sent klasörü

SMTP ile atılan mail çoğu sağlayıcıda Sent'e düşmez. Gönderim sonrası `imap-tools`
`mailbox.append(msg, 'Sent')` ile kopyası eklenir (Sent klasörü `\Sent` special‑use flag'inden
bulunur). Append başarısız olursa sadece uyarı loglanır — gönderim zaten olmuştur.

### Reply / bounce eşleştirme (IMAP)

`inbox.handle` bugün `gmail_thread_id` ile eşleştiriyor. Genelleştirilmiş sıra:
1. Gelen mailin `In-Reply-To` + `References` header'ları → `messages.rfc_message_id`
   (Message-ID'yi biz ürettiğimiz için güvenilir; Gmail yolunda da ek sinyal olarak çalışır).
2. Bounce ise `flufl.bounce` → başarısız adres(ler) → `leads.email`.
3. Mevcut fallback: göndericinin adresi → lead.

Polling: `UIDVALIDITY` + son görülen `UID` hesap başına saklanır; her tick'te `UID n+1:*` çekilir
(INBOX + varsa Junk). `UIDVALIDITY` değişirse tarih tabanlı aramaya düşülür; dedup zaten
`inbound` tablosunda.

---

## 4. Veritabanı (migration) — yapıldı (migration #4)

- `accounts` tablosu yeniden oluşturulmadı, kolon eklendi: `DROP TABLE accounts`,
  `enrollments.account_id` foreign key'i (`ON DELETE SET NULL`) yüzünden takip edilen inbox
  bilgisini silerdi. SMTP hesaplarında `refresh_token = ''`.
- Yeni kolonlar: `transport` (`gmail_api` | `smtp_imap`), `username`, `secret` (şifreli),
  `smtp_host/port/security`, `imap_host/port/security`, `save_sent` (NULL = host'a göre), `sent_folder`.
- `messages.gmail_id` / `gmail_thread_id` adları korundu; tüm transport'lar için provider id ve
  thread key'i tutuyor (yeniden adlandırma Faz 2'de inbox.py ile birlikte).
- Index: `messages(rfc_message_id)`.

---

## 5. Arayüz

Settings → "Inbox ekle" iki seçenek: **Google ile bağlan** (mevcut) / **E‑posta + şifre (SMTP/IMAP)**.
1. Kullanıcı e‑posta girer → `autoconfig.lookup()` host/port/TLS'i doldurur (düzenlenebilir).
2. Şifre/app password → **Bağlantıyı test et**: `transport.verify()` SMTP login + IMAP login +
   Sent klasörünü bulma. Başarısızsa sağlayıcıya özel ipucu (Gmail: "app password gerekir",
   M365: "Aralık 2026'dan itibaren OAuth gerekir").
3. Kaydet. Mevcut daily limit / pacing ayarları aynen geçerli.

Sheets okuma hâlâ en az bir Google OAuth bağlantısı istiyor — bu plan kapsamında değişmiyor,
UI'da bunu belirtiyoruz.

---

## 6. Fazlar

**Faz 1 — Transport katmanı + SMTP gönderim** (~2 gün) — ✅ yapıldı. Not: IMAP reply tespiti gelene kadar SMTP inbox'larında follow-up adımları gönderilmiyor, sequence durduruluyor.
- `transports/` iskeleti; Gmail adapter (davranış birebir aynı, mevcut testler yeşil kalmalı).
- `smtp_imap.send` + hata eşlemesi + Sent'e append.
- Migration.
- Testler: `aiosmtpd` Controller ile başarılı gönderim, 535, 4xx, 5xx, DATA sonrası kopma
  (custom handler ile), duplicate claim senaryoları.

**Faz 2 — IMAP okuma** (~3 gün) — ✅ yapıldı. UID ile artımlı okuma (durum `kv[imap_state:<id>]`'de, migration gerekmedi), INBOX + spam klasörü, `In-Reply-To`/`References` eşleştirme, flufl.bounce ile kalıcı/geçici bounce ayrımı. Follow-up kısıtı artık sadece IMAP sunucusu girilmemiş SMTP inbox'larında. GreenMail uçtan uca testleri: `tests/test_greenmail.py`.
- `imap-tools` ile UID polling, `thread_replies`, header tabanlı eşleştirme.
- `flufl.bounce` entegrasyonu (Gmail yolu da buna geçer → tek bounce mantığı).
- Testler: fixture `.eml` dosyalarıyla (gerçek Gmail/Outlook/Exchange/Postfix bounce örnekleri),
  UIDVALIDITY değişimi; opsiyonel GreenMail entegrasyon testi (`docker compose` profili).

**Faz 3 — UI + autoconfig + M365 OAuth** (~2–3 gün) — UI + autoconfig ✅ yapıldı (`smtp_web.py`, `autoconfig.py`, Settings → Inboxes formu, Domain health kartı + "Check now"). M365 XOAUTH2 henüz yok.
- Settings formu, autoconfig, "bağlantıyı test et".
- Microsoft XOAUTH2 (Entra app, `SMTP.Send` + `IMAP.AccessAsUser.All`), token yenileme
  `google.py` deseniyle.
- README / `.env.example` güncellemesi.

Toplam: ~1,5 hafta. Faz 1+2 bittiğinde Zoho/Yandex/kendi sunucu/Gmail app password ile
uçtan uca çalışır; M365 Faz 3 ile gelir.

---

## 7. Yeni bağımlılıklar

```toml
dependencies += ["imap-tools>=1.16", "flufl.bounce>=6.0"]
[dependency-groups] dev += ["aiosmtpd>=1.4"]
```

Hepsi Apache‑2.0 → projenin MIT lisansıyla uyumlu.

## 8. Açık sorular

- M365'i Faz 3'e bırakmak uygun mu, yoksa ilk müşteri M365 mi? (Öyleyse Faz 3'ün OAuth kısmı öne alınır.)
- GreenMail entegrasyon testlerini CI'da koşturmak istiyor muyuz (Docker gerektirir)?

---

## 9. Spam / deliverability testi

"Mail gitti mi" ile "inbox'a mı düştü" iki ayrı soru. İkincisi için de hazır araçlar var:

| Katman | Araç | Ne ölçer | Ne zaman |
|---|---|---|---|
| Domain kimliği (DNS) | **[checkdmarc](https://pypi.org/project/checkdmarc/)** (Python, 6.0.4) | SPF gönderen sunucuyu kapsıyor mu, DMARC var mı / politikası, MX, STARTTLS | `check-inbox` içinde, inbox bağlanırken |
| İçerik + header puanı | **[rspamd](https://docs.rspamd.com/)** (`rspamd/rspamd` Docker, `/checkv2` HTTP API) | Spam filtresinin vereceği puan + hangi kuralların tetiklendiği (büyük harfli konu, para işareti, kısaltılmış link...) | Test suite'te şablonlar için; ileride editörde "spam skoru" önizlemesi |
| Gerçek inbox yerleşimi | Kendi seed inbox'larımız (Gmail, Outlook, Yahoo) + Faz 2 IMAP okuma | Mail INBOX'a mı Spam/Junk'a mı düştü, alıcı tarafındaki `Authentication-Results` (SPF/DKIM/DMARC pass?) | `check-inbox --send-to <seed>` → IMAP'ten geri oku |
| Uzun vadeli itibar | Google Postmaster Tools, Microsoft SNDS, **[parsedmarc](https://pypi.org/project/parsedmarc/)** (DMARC raporları) | Domain/IP itibarı, şikâyet oranı, kimlik doğrulama hataları | Canlı gönderim başladıktan sonra |

Örnek ölçüm (12 Kasım 2026, rspamd 4.2, usechannelwise.com):
- Normal kampanya maili, Google sunucusundan gelmiş gibi: **−0.9** (SPF + DMARC pass)
- Aynı mail, SPF'de olmayan bir IP'den: **+9.4** — puanın neredeyse tamamı altyapıdan (SPF ihlali, reverse DNS yok)
- Spam'li içerik ("FREE MONEY!!! $$$"), Google'dan: **+3.1** (SUBJ_ALL_CAPS +3, SUBJECT_HAS_CURRENCY +1)

**Haftalık otomatik kontrol (yapıldı):** worker haftada bir `deliverability.run()` çalıştırıyor: gönderim yapılan her domain için SPF (gönderen sağlayıcıyı kapsıyor mu), DMARC politikası ve DKIM anahtarı. Sonuç `kv[deliverability]`'de, sorunlar aktivite loguna `deliverability` türüyle warning/error olarak düşüyor. Henüz yok: dashboard'da ayrı bir kart, rspamd ile içerik puanı, seed inbox yerleşim testi.

Sonuç: en büyük etken içerik değil, **gönderen sunucunun domain'in SPF/DKIM'inde olması**. Bu yüzden
SMTP inbox bağlanırken SPF/DMARC kontrolü (checkdmarc) zorunlu adım olmalı.

Domain notu: `usechannelwise.com` → SPF Google + Porkbun'u kapsıyor, DKIM (google selector) var,
DMARC `p=none` (izleme modu; itibar oturunca `quarantine`'e çekilmeli).
