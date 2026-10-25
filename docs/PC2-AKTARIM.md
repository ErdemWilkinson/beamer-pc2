# PC2'de Aç: Bu fork'un eklediği özellik

Bu repo, [kalkman-code/beamer](https://github.com/kalkman-code/beamer)'ın (GPL-3.0) bir fork'u.
Beamer'ın kendisi bir yazılım KVM: mouse ana bilgisayarın ekran kenarından geçince, klavye ve
mouse ikinci bilgisayarı kontrol etmeye başlar. Bu fork buna bir şey ekliyor: **ana bilgisayardan
ikinci bilgisayara dosya, klasör ya da uygulama "gönderip" orada, ikinci bilgisayarın kendi
donanımıyla açtırmak.**

> Çalışan bir programı canlı olarak başka bilgisayara taşımak mümkün değil (programın RAM'deki
> durumu o makinede kalır). Bu yüzden gönderilen şey **içerik** (dosya/klasör) ya da **başlatma
> isteği** (uygulama adı). PC2 bunu kendi kurulu uygulamasıyla, kendi CPU/GPU/RAM'iyle açar.

## Kurulum (tek satır)

Her iki PC'de PowerShell açıp şunu yapıştırın:

```powershell
irm https://raw.githubusercontent.com/ErdemWilkinson/beamer-pc2/main/kur.ps1 | iex
```

`kur.ps1` sırasıyla şunları yapar:
1. Python 3.11 ve Git yoksa `winget` ile kurar.
2. Repoyu `%USERPROFILE%\beamer-pc2` klasörüne indirir (zaten varsa günceller) ve bağımlılıkları kurar.
3. Güvenlik duvarı kurallarını yazar (bir kez yönetici izni ister). Bağlı ağ Herkese Açık ise Özel
   yapılıp yapılmayacağını sorar: sadece evinizin ya da güvendiğiniz ağ için "E" deyin.
4. Masaüstü, Başlat menüsü ve oturum açılışı için kısayol ekler, sonra Beamer'ı başlatır.

Güncellemek için aynı komutu tekrar çalıştırmanız yeterli.

## Nasıl kullanılır

İki bilgisayara da bu fork kurulur ve normal Beamer gibi eşleştirilir (6 haneli kod). Sonra:

| Ne yaparsın | Ne olur |
|---|---|
| Bir klasörü **sağ kenara sürükle**, kenarda beliren mavi şeride bırak | Klasör PC2'ye kopyalanır, PC2'de VS Code (ya da Antigravity) o klasörle açılır. Klavye ve mouse da PC2'ye geçer. |
| Bir dosyayı sürükle-bırak | PC2'de, o dosya türünün varsayılan uygulamasıyla açılır. |
| VS Code / Antigravity **kısayolunu** sürükle-bırak | Exe kopyalanmaz. PC2 **kendi** kurulu VS Code'unu başlatır. |
| Dosya/klasöre sağ tık → **"Open on &lt;PC2'nin adı&gt;"** | Sürükle-bırakla aynısı. (Windows 11'de "Daha fazla seçenek göster" altında.) |
| Tray simgesi → "Send files / Send a folder to the other PC…" | Dosya seçici açılır, seçilenler gönderilir. |
| Tray → "Open received files" | PC2'de gelenlerin durduğu klasörü açar. |

Gelen dosyalar PC2'de `%USERPROFILE%\Beamer Inbox\<tarih>_<iş>\` altına yazılır.

## Ayarlar: `transfer.json`

`settings.json`'ın yanında durur (Beamer'ın kendi şemasına karışmasın diye ayrı tutuldu). Yoksa
varsayılanlar kullanılır. Bozuk ya da yanlış tipte olan alan sessizce varsayılana döner.

```json
{
  "receive": true,
  "inbox": "",
  "editor": "code",
  "editor_path": "",
  "apps": {"Code.exe": "", "Antigravity.exe": "", "notepad.exe": "", "explorer.exe": ""},
  "excludes": ["node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", ".next", "dist", "build"],
  "max_bytes": 2147483648,
  "follow": true,
  "target": "",
  "menu": true,
  "drop_zone": true
}
```

| Alan | Anlamı |
|---|---|
| `receive` | Bu PC gelen gönderimleri kabul etsin mi |
| `editor` | Gelen klasör neyle açılsın: `code`, `antigravity` ya da `custom` (`editor_path` ile) |
| `apps` | Uzaktan **başlatılabilecek** uygulamalar. Değer boşsa yol otomatik bulunur (App Paths → bilinen klasörler → PATH) |
| `excludes` | Klasör gönderirken atlanacak alt klasörler |
| `max_bytes` | Bu PC'nin kabul edeceği en büyük gönderim (varsayılan 2 GB) |
| `follow` | Gönderim bitince klavye ve mouse karşı PC'ye geçsin mi |
| `target` | Birden fazla eşleşmiş PC varsa hangisine gönderilsin (makine id'si) |
| `menu` / `drop_zone` | Explorer sağ tık girdisi / kenardaki bırakma şeridi açık mı |

## Güvenlik

- Aktarım kendi portunda çalışır (**TCP 24823**) ama Beamer'ın link'iyle aynı şifrelemeyi kullanır
  (eşleşme token'ı + X25519 + HKDF-SHA256 + ChaCha20-Poly1305). Anahtarlar ayrı bir HKDF etiketiyle
  (`beamer-transfer-v1`) türetilir, yani input link'inin bir frame'i burada açılamaz, buradakiler de orada açılamaz.
- PC2, sadece **kendisini kontrol etmesine izin verdiği** (`allow_drive`) eşleşmiş makineden gelen
  gönderimi kabul eder.
- Gelen her yol kontrol edilir (`..`, sürücü harfi, `:`, ters bölü, ayrılmış adlar, kontrol
  karakterleri). Her dosyanın boyutu ve SHA-256'sı doğrulanır. İş, gizli bir hazırlık klasörüne
  yazılır, sadece her şey doğruysa yerine taşınır.
- **Gelen hiçbir program çalıştırılmaz.** Sadece zararsız olduğu bilinen türler doğrudan açılır
  (`.txt .md .log .csv .json .yaml .toml`, resimler, ses/video, `.pdf .docx .xlsx .pptx .zip`).
  Geri kalan her şey (`.exe`, `.bat`, `.ps1`, `.lnk`, makrolu Office dosyaları, `.html`, temalar,
  bilinmeyen uzantılar) Inbox'a kaydedilir ve Explorer'da seçili gösterilir, o kadar.
- Gelen her dosyaya **Mark-of-the-Web** (internet bölgesi) işareti konur. Böylece Office'in Korumalı
  Görünüm'ü ve SmartScreen, indirilmiş bir dosyaya davrandıkları gibi davranır.
- Gelen klasör editörde açılır. Klasördeki `.vscode/tasks.json` gibi şeylerin çalışıp çalışmayacağına
  VS Code'un **Workspace Trust**'ı karar verir: bu ayarı kapatma, `Beamer Inbox`'ı da güvenilir bir
  üst klasörün içine koyma.
- Sunucu, kimliğini kanıtlamamış bağlantıların eşleşmiş makinelerin işlerine yer bırakmamasına
  izin vermez. Çok yavaş ilerleyen işler kesilir. Eşzamanlı işler disk alanını birlikte rezerve eder.
  Yarım kalmış `.partial-*` klasörleri açılışta silinir.
- Gönderen taraf ağ paylaşımı yollarını (`\\sunucu\...`) okumaz (SMB kimlik bilgisi sızıntısını önlemek için).
- Uzaktan başlatma sadece uygulama **adıyla** olur, gönderenin argümanları geçirilmez. Ad,
  `apps` listesinde yoksa ya da PC2'de kurulu değilse istek reddedilir.

## Kod haritası

| Dosya | Görevi |
|---|---|
| `core/transfer.py` | Wire protokolü, yol doğrulama, manifest, gönderici (`send`), sunucu (`TransferServer`) |
| `win_app/launch_win.py` | Alıcı politikası (ne, neyle açılır), `transfer.json` ayarları |
| `win_app/send_win.py` | Bırakılanı sınıflandırma (kısayol → başlatma isteği), hedef seçimi, arka plan `Outbox`'ı |
| `win_app/transfer_feature.py` | Uygulamaya bağlama: drop zone penceresi, tray eylemleri, Explorer menüsü |
| `win_app/sender.py` | `drag_at_edge` kancası: kenara sürükleme olunca drop zone'u haber verir |
| `win_app/kvm_bridge_win.py` | Birkaç satırlık kanca: başlat/durdur, tray, `--send-to-peer`, `xfer1` capability |

Upstream'den güncelleme almak kolay olsun diye `kvm_bridge_win.py`'deki değişiklikler minimumda
tutuldu.

## Testler

```powershell
cd win_app
..\.venv\Scripts\python -m unittest discover -s tests
```

Yeni testler: `core/tests/test_transfer.py` (gerçek localhost soketleriyle uçtan uca),
`win_app/tests/test_launch_win.py`, `win_app/tests/test_send_win.py`.

## Elle doğrulanması gerekenler (iki laptopla)

1. Mouse sağa gidince PC2'ye geçiyor, klavye PC2'ye yazıyor, pano çalışıyor.
2. Bir proje klasörünü sağ kenara sürükle → PC2'de VS Code açılıyor. Görev Yöneticisi'nde yükün
   PC2'de olduğu görülüyor.
3. `.txt` dosyasına sağ tık → "Open on <PC2>" → PC2'de Not Defteri açılıyor.
4. VS Code kısayolunu sürükle → PC2'nin kendi VS Code'u başlıyor.
5. Büyük bir klasör giderken mouse takılmıyor (aktarım ayrı bağlantıda).

İlk çalıştırmada Windows Güvenlik Duvarı 24823 için izin isteyebilir. Beamer'ın
Connection sayfasındaki güvenlik duvarı onarımı bu kuralı da yazar.
