# ITLG Keeper

Penjaga klaim untuk **Interlink Labs (ITLG)**: klaim mining 4 jam, group mining 24 jam, dan pemulihan burn — dijalankan sebagai daemon async yang self-healing, dengan dashboard terminal dan kontrol lewat Telegram.

Ditulis ulang dari nol sebagai **arsitektur baru**, bukan port dari skrip satu-file. Kalau Anda mencari "bot.py yang sama tapi dirapikan", ini bukan itu: model eksekusinya berubah (tiga worker independen, bukan satu loop), penyimpanannya berubah (SQLite, bukan tumpukan JSON), dan semua keputusan error dibuat eksplisit lewat tipe, bukan `if "TOO_EARLY" in message`.

---

## Kenapa beda

| Aspek | Skrip legacy | ITLG Keeper |
|---|---|---|
| Eksekusi | satu `while True` + `sleep(10)`, semua fitur saling menunggu | satu task asyncio per fitur, tidur sampai deadline masing-masing |
| Jeda manusiawi | `sleep(30-120)` memblokir seluruh loop | jitter diterapkan per worker, tidak pernah menahan worker lain |
| State | `token.json`, `token-backup.json`, `claim_state.json`, `.bot.pid`, `.stop` | satu database SQLite (WAL) dengan tabel `tokens`, `claims`, `kv`, `events` |
| Timer | re-check API tiap 10 detik | deadline disimpan, API dipanggil saat mendekati jatuh tempo |
| Error | pencocokan substring pada pesan | taksonomi exception (`TransientError`, `RateLimited`, `AuthExpired`, …) |
| 429 / 5xx | diabaikan atau retry buta | `Retry-After` dihormati, backoff eksponensial + circuit breaker |
| Respons non-JSON | dianggap "belum bisa klaim" | `UpstreamUnavailable` — terlihat sebagai kegagalan, bukan timer palsu |
| Timer API | diasumsikan milidetik | normalisasi otomatis ms/detik, aman untuk nilai string |
| Token opaque | dianggap kedaluwarsa setiap panggilan → login berulang | TTL konservatif, 401 yang memaksa refresh |
| Refresh token | bisa balapan antar-pemanggil | `asyncio.Lock` + double-check, satu refresh untuk N pemanggil |
| Jumlah klaim | selisih saldo setelah `sleep(2)` | ambil dari respons bila ada; kalau tidak, polling saldo sampai berubah |
| Pool grup | berhenti di grup pertama yang claimable → total salah | menjumlahkan semua grup |
| Recovery | hanya jalan kalau klaim mining berhasil | worker sendiri, jadwal sendiri, batas per putaran |
| Kontrol | proses `tg_gateway.py` terpisah, PID file, `pgrep` | bot Telegram di proses yang sama, tanpa PID file |
| Rahasia | plaintext di `config.json` | TOML mode 600 **atau** environment variable (cocok untuk container) |
| Dry-run | tidak ada | `--dry-run` di semua perintah klaim |
| Tes | tidak ada | unit test untuk penandatanganan, timer, parsing OTP, alur klaim |

Hal-hal yang **tidak** diubah karena itu kontrak server, bukan pilihan desain: base URL, header perangkat (`x-device-id`, `x-date`, `x-content-hash`), urutan login, dan bentuk payload tiap endpoint.

---

## Arsitektur

```
                       ┌──────────────────────────────┐
   CLI (typer) ───────▶│          AppContext          │
   Telegram bot ──────▶│  settings · ledger · tokens  │
                       │  client · services           │
                       └──────────────┬───────────────┘
                                      │
        ┌─────────────────────────────┼─────────────────────────────┐
        ▼                             ▼                             ▼
  MiningWorker                  GroupWorker                 RecoveryWorker
  (deadline 4 jam)              (deadline 24 jam)           (tiap 30 menit)
        │                             │                             │
        └──────────────┬──────────────┴──────────────┬──────────────┘
                       ▼                             ▼
                MiningService                 GroupService / RecoveryService
                       │                             │
                       └──────────┬──────────────────┘
                                  ▼
                             ApiClient (httpx)
                    signing · retry · envelope · breaker
                                  │
                                  ▼
                     prod.interlinklabs.api /api/v1
```

Tiga lapisan, satu arah dependensi:

1. **Transport** (`http.py`) — tahu soal header, envelope, retry, 401. Tidak tahu soal mining.
2. **Domain** (`services/`) — tahu soal alur klaim. Tidak tahu soal jadwal.
3. **Orkestrasi** (`scheduler.py`) — tahu soal kapan. Tidak tahu soal cara klaim.

Konsekuensinya: menambah fitur baru = satu service + satu worker. Tidak ada file 1.400 baris yang harus dibaca utuh.

---

## Instalasi

Butuh Python 3.10+.

```bash
git clone https://github.com/0xDimzy/itlg-keeper.git
cd itlg-keeper
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .
```

Atau tanpa clone:

```bash
pipx install git+https://github.com/0xDimzy/itlg-keeper.git
```

Cek pemasangan:

```bash
itlg-keeper --help
```

---

## Konfigurasi

### Cara cepat

```bash
itlg-keeper init          # tanya interaktif, tulis ./itlg-keeper.toml (mode 600)
itlg-keeper login         # ambil sesi pertama
itlg-keeper doctor        # pastikan semua hijau
itlg-keeper run
```

### File TOML

```toml
[account]
login_id     = "12345678"        # ID numerik Interlink, bukan email
passcode     = "123456"
email        = "you@gmail.com"   # opsional, untuk login OTP
imap_password = "abcd efgh ijkl mnop"  # App Password Gmail, bukan password akun
face_photo   = ""                # opsional, alternatif login selfie

[telegram]
token   = "123456:ABC..."        # opsional
chat_id = "123456789"

[runtime]
timezone    = "Asia/Jakarta"
jitter_min  = 45                 # detik, jeda manusiawi sebelum klaim
jitter_max  = 180
dry_run     = false
log_level   = "INFO"

[device]
device_id  = "…"                 # dibuat oleh `init`, jangan diubah sembarangan
brand      = "XiaoMi"
model      = "M2101K6G"
user_agent = "okhttp/4.12.0"
```

### Environment variable

Semua kunci punya padanan env, jadi tidak perlu menyimpan rahasia di disk:

| Env | Padanan |
|---|---|
| `ITLG_LOGIN_ID`, `ITLG_PASSCODE` | `account.login_id`, `account.passcode` |
| `ITLG_EMAIL`, `ITLG_IMAP_PASSWORD` | `account.email`, `account.imap_password` |
| `ITLG_FACE_PHOTO` | `account.face_photo` |
| `ITLG_TELEGRAM_TOKEN`, `ITLG_TELEGRAM_CHAT_ID` | `telegram.*` |
| `ITLG_DATA_DIR` | folder state (default `~/.local/share/itlg-keeper`) |
| `ITLG_DRY_RUN`, `ITLG_LOG_LEVEL`, `ITLG_TIMEZONE` | `runtime.*` |
| `ITLG_CONFIG` | path file TOML |

Urutan prioritas: **environment → TOML → default**.

### Login

Dua jalur, keduanya menghasilkan access + refresh token:

* **OTP email** — butuh `email` + `imap_password`. Aktifkan 2FA Gmail, buat *App Password* 16 karakter, lalu tempel apa adanya (spasi boleh).
* **Selfie** — butuh `face_photo`. Kirim `--face --photo selfie.jpg`. Ukuran 50 KB – 5 MB, JPEG atau PNG (tipe dideteksi dari magic bytes, bukan dari ekstensi).

```bash
itlg-keeper login                 # otomatis pilih OTP kalau dikonfigurasi
itlg-keeper login --face --photo ~/selfie.jpg
```

Token disimpan di database dan diperbarui otomatis. Login ulang hanya terjadi kalau refresh token ditolak.

---

## Menjalankan

```bash
itlg-keeper run                   # daemon + bot telegram
itlg-keeper run --dry-run         # amati tanpa mengirim klaim
itlg-keeper run --once            # satu putaran, keluar (cron)
itlg-keeper run --no-telegram     # tanpa bot perintah
```

### systemd

```ini
# /etc/systemd/system/itlg-keeper.service
[Unit]
Description=ITLG Keeper
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=itlg
EnvironmentFile=/etc/itlg-keeper.env
ExecStart=/usr/local/bin/itlg-keeper run
Restart=always
RestartSec=15
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/itlg-keeper

[Install]
WantedBy=multi-user.target
```

### Docker

```bash
docker compose up -d
```

`docker-compose.yml` sudah menyiapkan volume untuk database dan variabel environment; tidak ada rahasia yang tertanam di image.

---

## Perintah

| Perintah | Fungsi |
|---|---|
| `init` | tulis konfigurasi awal secara interaktif |
| `login` | ambil sesi (OTP atau selfie) |
| `run` | daemon: 3 worker + bot Telegram |
| `status` | dashboard sekali jalan (`--json` untuk monitoring) |
| `claim -k mining\|group\|recovery\|all` | klaim sekarang, `--force` dan `--dry-run` tersedia |
| `devices [--rotate]` | lihat/ganti fingerprint perangkat |
| `doctor` | diagnosa konfigurasi, jaringan, sesi, jam |
| `export -o status.json` | ekspor status untuk Prometheus/Grafana dsb. |
| `config` | cetak konfigurasi efektif (secret disamarkan) |
| `prune --days 30` | bersihkan riwayat lama |

Contoh `status`:

```
╭─ ITLG Keeper ─────────────────────────────────────╮
│         Akun  0xDimzy                             │
│        Saldo  1,284.5531 ITLG                     │
│         Laju  harian 12.4 · grup 45.0 · referral 7 │
╰───────────────────────────────────────────────────╯
╭─ Jadwal ──────────────────────────────────────────╮
│ frame 4 jam: buka dalam 2j 41m  (10 Nov 22:00:12)  │
│ group 24 jam: buka dalam 6j 12m  (pool 45.0)       │
│ pemulihan: 2 burn · 3.5 ITLG                       │
╰───────────────────────────────────────────────────╯
```

---

## Cara kerja klaim

**Mining 4 jam.** Keeper membaca `check-is-claimable`; kalau frame sudah terbuka ia menunggu jitter, membaca saldo, memanggil endpoint iklan, menunggu `timeRetry`, lalu `POST /token/claim-airdrop`. Hadiah diambil dari respons bila server menyertakannya; kalau tidak, saldo dipolling sampai berubah. `TOO_EARLY` tidak dianggap error — itu artinya klaim dilakukan dari HP, jadi timer diselaraskan.

**Group 24 jam.** Semua grup dijumlahkan untuk laporan pool, lalu satu grup claimable dipakai sebagai pemicu karena satu panggilan melunasi semuanya. `ALREADY_CLAIMED` bersifat jinak.

**Recovery.** Dijalankan sebagai worker sendiri setiap 30 menit dengan batas 25 burn per putaran. Nilai yang dicatat berasal dari respons klaim; kalau tidak ada, dipakai nilai dari daftar burn.

**Ketahanan.** Error transien → backoff eksponensial (maks 30 menit). Rate limit → ikuti `Retry-After`. Enam kegagalan transport berturut-turut → circuit breaker 60 detik. Error autentikasi → notifikasi Telegram dan jeda 5 menit sebelum mencoba lagi. Worker yang mati di-restart supervisor.

---

## Bot Telegram

Isi `telegram.token` dan `telegram.chat_id`. Hanya chat id itu yang dilayani; pesan dari orang lain diabaikan tanpa balasan.

```
/status   ringkasan akun, jadwal, dan klaim hari ini
/claim    klaim mining sekarang
/group    klaim group mining
/recover  pulihkan burn
/pause    hentikan klaim otomatis
/resume   lanjutkan
/help     daftar perintah
```

Bot berjalan di proses yang sama dengan scheduler, jadi `/pause` benar-benar menghentikan worker (bukan hanya menulis file penanda).

---

## Data & privasi

Semua state ada di satu folder (`ITLG_DATA_DIR`, default `~/.local/share/itlg-keeper`):

```
keeper.db        SQLite: token, riwayat klaim, event, identitas perangkat
```

Tidak ada telemetri. Tidak ada data yang dikirim ke pihak ketiga selain `prod.interlinklabs.ai` dan, bila diaktifkan, `api.telegram.org`. `config` dan `status --json` menyamarkan secret.

---

## Troubleshooting

| Gejala | Sebab & solusi |
|---|---|
| `login ID tidak terdaftar` | `login_id` harus angka ID Interlink, bukan email |
| OTP tidak pernah datang | App Password salah, IMAP dimatikan, atau email berbeda dengan yang terdaftar di akun |
| `respons bukan JSON` | halaman proxy/Cloudflare di depan API; cek jaringan, bukan konfigurasi |
| `circuit breaker terbuka` | API sedang tidak merespons; tunggu satu menit, keeper mencoba sendiri |
| Klaim "sudah diklaim" terus | ada perangkat lain yang mengklaim; timer sudah diselaraskan otomatis |
| Timer tidak masuk akal | jalankan `itlg-keeper doctor`, bandingkan jam server |
| `device` berubah sendiri | jangan edit bagian `[device]`; kalau perlu ganti, pakai `devices --rotate` (sesi akan direset) |

---

## Pengembangan

```bash
pip install -e ".[dev]"
ruff check src tests
mypy src
pytest
```

Tes tidak pernah menyentuh jaringan: `httpx.MockTransport` dipakai untuk mensimulasikan envelope sukses, `TOO_EARLY`, 429, dan halaman HTML.

---

## Lisensi & kredit

MIT. Lihat [LICENSE](LICENSE).

Kontrak API tidak terdokumentasi publik; bentuk endpoint dan header di sini berasal dari observasi komunitas, termasuk repo `feb-frmn/itlg-claim` yang lebih dulu memetakannya. Kode di repo ini ditulis ulang sepenuhnya — tidak ada baris yang disalin.

> **Catatan pemakaian.** Ini alat otomasi untuk akun Anda sendiri. Automasi dapat melanggar ketentuan layanan Interlink Labs dan berisiko akun ditangguhkan. Pakai dengan sadar, dan jangan pernah menjalankan lebih dari satu instance untuk akun yang sama.
