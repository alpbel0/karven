# Task 3.2: ilişki test motorunun kalibrasyon sonucu

**Ana cümle:** Kalibrasyon adaylarının ayarlama değerlendirmesi tamamlandı; protokol v1.5 kabul için
**aday belirleyemedi; bağımsız doğrulama yapılmadı.** Bu, "yöntem geçersiz" demek değildir: ön-kayıtlı
ölçütleri sağlayamamak bu daha güçlü iddiayla eşdeğer değildir. Bu belge ne ölçüldüğünü, motorun nasıl
teslim edildiğini ve ne iddia **edilmediğini** yazar.

## Teslim edilen durum

| | |
|---|---|
| Motor altyapısı | **Teslim edildi** (`backend/app/analysis/`): frekans eşleştirme, dönüşümler, TÜFE ile reel hale getirme, gecikme arama, iki pencere, regresyon, durum, graf adaptörü, komut satırı. Gerçek veride uçtan uca çalıştı (USD/TL → TÜFE; canlı konteynerde de, imaj yeniden derlendi). |
| İstatistiksel doğrulama | **Açık kaldı.** Doğrulanmış bir anlamlılık yöntemi yok. |
| Varsayılan yöntem | **Deneysel**: `WholeChainBootstrap(blok = 24 ay, B = 499)`, ayar sonuçlarına bakılarak seçildi (protokolün seçtiği bir yöntem değildir). `calibrated=False`. |
| Varsayılan kapı | **Deneysel**: `n ≥ 100` ve (gecikme-12 otokorelasyonu > 0,3 **ve** gecikme-24 > 0,2) ise pencere "kapıdan geçmedi". Kapıdan geçmek bir doğrulama **değildir**. |
| Sonuç etiketi | Her sonuç `reliable=False` (gerekçe: "anlamlılık yöntemi doğrulanmadı"). Kapı sonucu, doğrulanmama ve yapısal uygunluk **ayrı alanlarda** durur: `details.method_validated`, `details.gate_passed`, `details.structurally_eligible`. |

**Tüketici kuralı (kodda zorlanır):** `RelationRecord.is_reliable_support` yalnız `status == supported`
**ve** son test `reliable=True` ise doğrudur. Deneysel `supported` kayıtlar sonraki ajanlarca
yerleşik bilgi olarak kullanılmamalı; admin ekranı "deneysel istatistiksel destek, yöntem doğrulanmadı"
göstermelidir (Task 3.4/5.2). Yapısal olarak karar verilemeyen pencere hiçbir koşulda güvenilir sayılmaz.

## Ne yapıldı (özet; ayrıntı `PROTOCOL.md`)

1. **İlk plan** (v1.0): bir yöntem seçmek, sahte alarm oranı ölçmek, doğrulamak. İki bağımsız inceleme
   (Codex dahil) planı ve ardından kodu gözden geçirdi; on kod bulgusu düzeltildi (özellikle varyans/ortalama
   kırılmasının atılan ısınma bölümüne yerleştirilmesi).
2. **Ayar-1** (v1.2, 513 hücre x 200 tekrar, 25 aday): hiçbir aday geçmedi. Sebep: kalıcı deterministik
   mevsimsellik ailesi (`seasonal_indep`, kapısız %50'ye varan yanlış destekleme). O ailenin dışında zincir
   tabanlı adaylar bölge hücrelerinin hepsinde ≤ %5'ti; HAC çeşitleri %78-96'sında.
3. **v1.3 / v1.4**: kapsam daraltma ve **gecikme-24 kapı özelliği** (aynı hücreler ve tohumlarla belirli
   olarak yeniden hesaplandı). Bunların **hepsi ayar sonuçları görüldükten sonra** yapıldı ve
   protokolde böyle yazılıdır (v1.2 ve v1.3 başarısızlıkları silinmedi).
4. **Ayar-2** (v1.5 ön-kayıtlı, n ∈ {100, 250}; n=100'de 800, n=250'de 600 tekrar): önceden kilitlenmiş
   güvenlik paylı uygunluk filtresi hiçbir (yöntem, kapı) çiftini uygun bulmadı; ön-kayıtlı tek geri çekilme
   (`sar12_05` reddedilmeli sınıfına) da yetmedi → **"dondurulacak aday yok"**, doğrulama (tohum 9001)
   koşulmadı.

## Ön-kayıt hatası (erratum)

Üç sürücülü regresyon, motorun en-az-gözlem kuralıyla (`eşik × sürücü sayısı`, aylıkta 36 × 3 = 108) n = 100'de
**yapısal olarak** karar veremez; k = 3 bölge hücreleri (15 hücre) her adayda 0 güvenilir tekrar verir. Ön-kayıt
"her bölge hücresinde ≥ 200 güvenilir tekrar" derken bunu hesaba katmadı: filtre bu hücrelerde veriden bağımsız
olarak başarısızdır. Bu hata **açıklayıcı** olarak kaydedilir: bu hücreleri dışarıda bırakıp aynı ayar verisini
yeniden çözümlemek (aşağıdaki tablolar öyle de okunabilir) kabul kararını değiştirmez, çünkü gerçek
başarısızlıklar sürer (aşağıya bakın). Özgün sonuç yerine geçmez.

## Ölçülen hata profili (deneysel varsayılan yöntem, tuning verisi)

Tablolar `RESULT-tables.md` içindedir (`python -m app.analysis.calib.report`); hepsi nihai iki-pencere kararı ve
iki pencerenin de kapıdan geçtiği tekrarlar üzerindendir. **Bunlar ayar verisidir, doğrulama kanıtı değildir.**

Özet (chain_24, deneysel kapı):
- **Yanlış destekleme** (ilişki yokken ya da yalnız bir sürücü gerçekken "destekleniyor"), bölge aileleri
  için toplu: **%0,1 - %2,6**; tek bir sabit testin ideali ≈ %2,5. En kötü hücreler: `seasonal_weak` k=1 n=100
  **%4,9** (797 güvenilir tekrar), `sar12_05` k=1 n=100 %4,6, `sar12_05` k=2 (r = 0,8) n=100 %4,4.
- **Kapının reddetmesi gereken aileler** (kalıcı mevsimsellik): sızıntı ≤ %2,2, güvenilir pay ≤ %6,4
  (`seasonal_common` n=100); `sar12_08` hiç sızmadı.
- **Güvenilir işaretleme payı:** iid ≈ %78 (n=100), %100 (n=250); ar05 ≈ %77 / %99,5; seasonal_weak
  ≈ %77 / %99; **ma11 (aylık veride yıllık değişim, ana kullanım durumu) yalnız %49 / %60**;
  `sar12_05` %25 / %6,5; k = 3 ve n = 100'de yapısal olarak hiç.
- **Güç** (gerçek ilişki, güvenilir sonuçlar arasında): tek sürücü güçlü etki %92, orta %69-73, zayıf %21;
  iki sürücü güçlü %88, orta %55-61; üç sürücü n=250 %89.
- **HAC** (OLS + Newey-West) ayar-1'de bölge ailelerinde kuyruğunda %9-15'e varan yanlış destekleme
  gösterdi (toplu %2-3, ama kuyrukta kötü); bu yüzden varsayılan değildir.

## Bilinen zayıf bölgeler (ürün riskleri)

- **Aylık veride yıllık değişim (ma11)**: pencerelerin ancak yarısı güvenilir sayılabilir; kalıcı mevsimsellik
  kapısıyla lag-12/lag-24 ayrımı bu ailede yanılabilir.
- **Zayıf mevsimsel veri ve 100 civarı gözlem**: %5 civarı yanlış destekleme (kapı bunu göremez).
- **Üç sürücü** ancak ≥ 108 gözlemde karar verir; ikinci pencere (2017-2026, aylıkta 117 ay) k = 3 için
  yetersiz kalabilir.
- **Ham mevsimli seriler**: kapı yalnız kalıcı döngüsel mevsimselliği (lag-12 ve lag-24 yüksek) yakalar; zayıf
  ya da değişen mevsimsellik, ortalama kırılması ve varyans kırılması için güvence **yoktur** (kapının kırılma
  eşikleri sonsuz bırakıldı).
- **Kalıcı ama mevsimsel olmayan seriler kapıyı yanlışlıkla tetikleyebilir.** Canlı denemede (USD/TL kuru → TÜFE
  yıllık değişim) tüm yıllar penceresi "kalıcı mevsimsellik" diye işaretlendi (lag-12 otokorelasyonu 0,67, lag-24
  0,51): uzun süreli bir yükseliş döneminin yıllık değişimi mevsimsel olmadan da bu kalıbı verir. Kapı gerçek
  mevsimselliği, kalıcı dönemsel rejimlerden ayıramaz; bu durumda sonuç yalnızca bilgi amaçlı işaretlenir.
- **Nedensellik:** istatistiksel destek nedensellik kanıtı değildir.

## Ne iddia edilmiyor

- "Yöntem kalibre edildi", "yanlış destekleme ≤ %5 doğrulandı" ya da "kapıdan geçen sonuç güvenilirdir".
- Ayar sonrası seçilen yöntem ve kapının doğrulama kanıtı olduğu.
- Grafa yazılan `supported` ilişkilerin yerleşik bilgi olduğu.

## Yeniden deneme

Ancak **yeni protokol sürümü** ile: önceden kilitlenmiş yeni kapsam (yapısal olarak mümkün hücreler dahil),
yöntem/seçim kuralı, kabul ölçütleri ve **kullanılmamış tohumlar** (doğrulama kümesi 9001 hiç açılmadı ve
kullanılabilir). Tavanı %5'ten yükseltmek ya da n = 100'ü dışlayıp n ≥ 120 kapsamı kurmak teknik düzeltme
değil, **ürünün kabul ettiği riskin değişmesi** ve kullanıcı kararıdır. Eski ailelerden öğrenildiği için
yeni parametre bileşimleri ve zorlayıcı mekanizmalar eklenmelidir. Ham veriler `backend/calibration_runs/`
altındadır (git dışı); tohumlarla yeniden üretilir.
