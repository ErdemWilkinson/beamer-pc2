# Uzat modu: PC2'yi ana PC'nin ikinci ekranı yapmak (plan)

**Hedef:** Ana PC'de tek bir **"Uzat modu"** düğmesine basınca:
- PC2'nin ekranı ana PC'nin **ikinci monitörü** gibi davranır, tıpkı Windows'taki Win+P → **Uzat** gibi.
- Pencereler ana PC'den PC2'nin ekranına sürüklenebilir.
- Görüntü, ana PC'nin ekran kartıyla (RTX 4050, NVENC) sıkıştırılıp 60 fps akar.
- Düğmeye tekrar basınca her şey eski haline döner. PC2 kendi masaüstüne, Beamer de normal geçişe döner.

**Beamer'dan farkı:** Beamer'da her PC kendi programlarını kendi donanımında çalıştırır; paylaşılan yalnızca mouse ve klavyedir. Uzat modunda PC2 yalnızca bir **ekran** olur ve o ekrandaki her şey ana PC'nin donanımında çalışır. İki mod birbirini tamamlar.

## Parçalar

| Nerede | Ne | Neden | Kurulum |
|---|---|---|---|
| Ana PC | **Virtual Display Driver (VDD)** | Windows'a sahte bir ikinci monitör ekler, masaüstü onun üzerine uzar | `winget install --id=VirtualDrivers.Virtual-Display-Driver -e` (yönetici) |
| Ana PC | **Sunshine** | Sanal monitörün görüntüsünü ekran kartıyla kodlayıp yayınlar | `winget install --id=LizardByte.Sunshine -e` (yönetici) |
| PC2 | **Moonlight** | Yayını tam ekran gösterir, PC2'nin mouse'unu ve klavyesini ana PC'ye iletir | `winget install --id=MoonlightGameStreamingProject.Moonlight -e` |

Üçü de açık kaynak. Sunshine ve Moonlight GPL-3.0 lisanslı (Beamer'la aynı). Tersine mühendislik gerekmiyor: belgelenmiş arayüzlerini kullanıyoruz.

## Beamer'ın "Uzat modu" düğmesinin yapacakları

1. **Ana PC'de** sanal monitörün açık olduğunu denetler. Sunshine'a, web arayüzü (`https://localhost:47990/api/config`, `output_name`) üzerinden, o monitörü yayınlamasını söyler.
2. Beamer'ın şifreli aktarım kanalıyla **PC2'ye** "Uzat modunu başlat" mesajı gönderir. PC2 şunu çalıştırır:
   `Moonlight.exe stream <ana PC> "Desktop" --display-mode fullscreen --fps 60 --absolute-mouse --capture-system-keys fullscreen --no-audio-on-host`
3. Mouse ve klavye geçişini **duraklatır**. Ana PC'nin mouse'u PC2'nin ekranına artık doğal yoldan, ikinci monitöre geçer gibi geçer.
4. Kapatınca: PC2'de `Moonlight.exe quit <ana PC>`, sanal monitör kapatılır, Beamer'ın geçişi yeniden açılır.

**Bir kerelik eşleştirme:** PC2'de Moonlight ilk açılışta 4 haneli bir kod gösterir. Bu kod ana PC'de Sunshine'ın web sayfasına (`https://localhost:47990` → PIN) girilir. Beamer bunu ileride `POST /api/pin` ile kendisi yapabilir.

## Gecikme ve kalite (beklenen)
- **Görüntü:** 60 fps, 1080p, ~20-40 Mbps. NVENC ile kodlama ~5 ms; PC2'deki Intel GPU'nun H.264/HEVC çözmesi ~5-10 ms.
- **Wi-Fi:** PC2'nin Wi-Fi'ındaki 100-300 ms'lik takılmalar burada da görüntüde takılma olarak görünür. Moonlight bunları kısmen telafi eder, ama Ethernet kablosu yine en iyi sonucu verir.

## Riskler ve geri alma
- **VDD bir ekran sürücüsü.** Belgeleri, büyük bir ekran kartı ya da yonga seti güncellemesinden önce kaldırılmasını öneriyor. Siyah ekran gibi nadir bir sorunda Güvenli Mod'dan kaldırılır. Bu yüzden kurulum yalnızca kullanıcının onayıyla yapılacak.
- **Kaldırma:** `winget uninstall VirtualDrivers.Virtual-Display-Driver`, `winget uninstall LizardByte.Sunshine` (ana PC); `winget uninstall MoonlightGameStreamingProject.Moonlight` (PC2).

## Uygulama sırası
1. Kullanıcı onay verir. Ana PC'ye VDD ve Sunshine, PC2'ye Moonlight kurulur, bir kerelik eşleştirme yapılır.
2. Elle deneme: Win+P → Uzat ile sanal monitör açılır, PC2'de Moonlight → Desktop. Gecikme ve kalite ölçülür.
3. Beamer'a `win_app/extend_mode_win.py` eklenir: Sunshine API istemcisi, sanal monitör denetimi, PC2'ye "başlat/durdur" mesajı ve tepsi düğmesi.
4. Testler ve iki PC'de doğrulama.
