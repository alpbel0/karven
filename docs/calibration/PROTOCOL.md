# Anlamlılık yöntemi kalibrasyon protokolü (Task 3.2, Parça 2)

**Sürüm 1.5 (2026-10-07). Sonuç ve eratum aşağıda §16; ayar-2 sonucu: dondurulacak aday yok, doğrulama yapılmadı.** Ayar aşamasının her şeyini kilitler. Ayarda belirlenecek unsurlar
(yöntem seçimi, güvenilirlik kapısı eşikleri, yöntem ayarları) ayar bitince **dondurma kaydına**
(`FREEZE.md`) yazılır; doğrulama yalnızca o kayıtla çalışır. Kilitli bir maddeyi değiştirmek yeni
protokol sürümü ve yeni doğrulama gerektirir. Bu protokol istatistiksel desteği ölçer, nedensellik
kanıtı değildir.

## 1. İşlem sırası (gerçek motorla aynı)

`reel hale getir (TÜFE, nominal TL serileri) → frekans eşleştir (en düşük frekans, eksik dönem atılır)
→ dönüşüm uygula → pencereyi kes (en uzun kesintisiz blok) → gecikme ara → test → iki pencere →
durum`. Kalibrasyon `engine.prepare` ve `engine.run_window` fonksiyonlarını kullanır; başka bir zincir
kurulmaz.

## 2. Sıfır hipotezleri

- **İkili (k = 1):** H0 = sürücü ile hedef arasında, hipotezin gecikme aralığının **hiçbir** gecikmesinde
  doğrusal/ilintili bağımlılık yok. (Bağımsızlık temelli yöntemler bunu test eder; OLS/HAC "gecikme
  aralığında seçilmiş gecikmenin katsayısı sıfır" der. İkisi aynı hipotez değildir ve sonuçlar birlikte
  raporlanır.)
- **Çoklu (k ≥ 2):** her sürücü j için H0_j = j. sürücünün, diğer sürücüler **kendi aday gecikme
  aralıklarında** modele alındığında, hedefe aday gecikmelerin **hiçbirinde** katkısı yok.
- **Kısmi sıfır:** en az bir j için H0_j doğru, en az biri için yanlış. Kuralımız **her** sürücünün
  katkısını göstermeyi istediği için kısmi sıfırda "destekleniyor" **yanlış destekleme**dir.
- **Ters etki:** gerçek etki beklenen yönün tersindeyse de "destekleniyor" yanlıştır.

## 3. Karar kuralı (kilitli)

- Pencere sonucu `supported` = her sürücü için tahmin edilen işaret beklenen yönde **ve** **iki taraflı**
  p < 0,05. Bootstrap/kaydırma p'si de iki taraflıdır: p = (|T*| ≥ |T_gözlenen| olan sayı + 1) / (B + 1)
  (kaydırmada B yerine kullanılabilen kaydırma sayısı). İşaret şartı ayrıca uygulanır. Tek taraflı test
  ayrı bir çalışmaya ertelenmiştir.
- Gecikme: sürücü başına hipotez aralığında en güçlü |r| (ikili aramadan).
- Minimum gözlem: aylık 36, üç aylık 12, yıllık 10, yarıyıllık 18; regresyonda eşik × sürücü sayısı.
- İlişki durumu: iki pencere `supported` → supported; ikisi `unsupported` → unsupported; biri
  supported biri unsupported → periods_differ; herhangi biri `insufficient_data` → insufficient_data.
- **Nihai sonuç yalnızca iki pencere de güvenilirse güvenilirdir.**
- "Veri yetersiz" (yapısal koşul sağlanmıyor) ile "güvenilmez" (hesap yapıldı, hata kontrolü için
  dayanak yok) ayrı alanlardır.

## 4. Ölçütler

- **Tekrarın birimi:** tek üretilmiş veri seti ve onun **nihai** kararı (iki pencere dahil). İki pencere
  ayrı tekrar sayılmaz. İki pencere tek uzun seriden kesilir (2005-01'den başlayan, pencereler iç içe).
- **Birincil:** hücre başına `P(destek | H0 veya kısmi H0 veya ters etki, nihai sonuç güvenilir)` =
  `güvenilir ve yanlış destekleyen tekrar / güvenilir tekrar`. **Tavan %5** (kullanıcı kararı
  2026-10-06; bu bir risk toleransıdır, tek bir sabit testin ideal %2,5'ine kalibre olduğu iddiası değil).
- **Raporlanır:** güvenilir işaretleme oranı, güç (güvenilir ve toplam), yetersiz veri oranı, doğru gecikme
  oranı, katsayı güven aralığı kapsamı, iki pencerenin ayrı sonuçları (iki pencere bağımsız doğrulama gibi
  sunulmaz), başarısız hesap sayısı.
- **İddia edilmez:** P(H0 | destek).

## 5. Simülasyon katmanları ve senaryolar

**A. İstatistik katmanı** (doğrudan durağan süreçler, katsayılar bilinir; testler dönüşüm uygulanmadan):
- Yenilikler `y_t = β·x_{t-lag} + ε_t`, birim varyanslı. Etki büyüklüğü β ∈ {0 (sıfır), 0,2 (zayıf),
  0,4 (orta), 0,7 (güçlü)}; ters etki β = −0,4 (beklenen yön pozitif).
- **Ayarlama aileleri:** bağımsız N(0,1); AR(1) φ ∈ {0,5; 0,9}; ortak mevsimsel bileşen **var** ve **yok**
  (ayrı senaryolar); varyans kırılması (ikinci yarıda ×2 sapma); ortalama kırılması (ortada +1 sapma);
  çakışma yapısı MA(11) (aylık bağımsız büyümenin 12 aylık toplamı). Isınma: 200 gözlem atılır;
  standartlaştırma teorik varyanstan. Ağır kuyruk (t₃) yalnızca bu katmanda, üstel dönüşüm olmadan.
- **Doğrulamada görülmemiş aileler:** ARMA(1,1) φ = 0,6, θ = −0,5; AR(1) φ = −0,5; sürücüye bağlı koşullu
  varyans σ_t = exp(0,3·x_t); yarı yapay aile (yenilikler gerçek serilerden alınan artıkların **bağımsız**
  çekilişleri, sıfır kurgu gereği doğru).
- Sürücü yapıları: k = 1 (sıfır, gerçek, ters); k = 2 (sıfır-sıfır, **gerçek-sıfır kısmi**, gerçek-gerçek,
  gerçek-ters, sürücüler arası ρ ∈ {0; 0,5; 0,8} ile gerçek-sıfır); k = 3 (gerçek-sıfır-sıfır,
  gerçek-gerçek-sıfır, gerçek-gerçek-gerçek). Gerçek gecikme 1 (ikinci sürücü için 2); aralık 0–3.
- Gözlem sayısı n (dönüşüm, hizalama, gecikme sonrası): 36, 60, 120, 250.

**B. Uçtan uca katman** (seviyeler `100·exp(cumsum(büyüme·0,01))`, tüm zincir):
- Dönüşümler: yıllık % değişim; dönemsel % değişim ve fark **stres senaryosu** (seviye çarpımsal,
  varyans durağan değil; sıfır hipotezi bozulmaz ama varsayımlar zorlanır).
- Ek: boşluklu seri (ardışık %10 eksik ay), ortak TÜFE bileşenli nominal seriler, mevsimsel ham seri.
- n_after ∈ {60, 120, 250}.

## 6. Aday yöntemler ve tam tanımları

Hepsi aynı sentetik verilerde (paylaşılan veri, bağımsız yeniden üretilebilir rastgele akışlar:
`numpy.random.SeedSequence.spawn`). Yeni yöntem eklenmez.

1. **HAC:** OLS + Newey-West (Bartlett, küçük örneklem düzeltmesi, t). `maxlags` ∈ {kural,
   `12/adım − 1` örtüşme ufku, 18/adım, 24/adım} (adım = dönem ay sayısı). Gecikme düzeltmesi
   (denenen gecikme sayısıyla Bonferroni, p ≤ 1) kapalı/açık. İstatistik: katsayının HAC t'si.
2. **Dairesel kaydırma (yalnız k = 1):** istatistik = hipotez aralığındaki en güçlü |r|; sürücü
   `kaydırılır`, her kaydırmada gecikme araması baştan yapılır. `guard` = 12 ay / adım dönem (sıfıra bu
   kadar yakın kaydırmalar dışlanır), yerine koymadan çekim, en çok 199, kullanılabilir kaydırma azsa
   hepsi. p = (|T*| ≥ |T| + 1) / (kullanılan + 1).
3. **Durağan bootstrap (yalnız k = 1):** sürücü yeniden örneklenir, hedef sabit; istatistik kaydırmadakiyle
   aynı (en güçlü |r|, tüm gecikme araması her yeniden örneklemede). Ortalama blok uzunluğu ∈ {12, 18, 24}
   ay/adım dönem ve Politis–White (`arch.bootstrap.optimal_block_length`, **sürücü dizisine** uygulanır,
   yuvarlama: yukarı tam sayı, sınır [2, n/3], hesaplanamazsa 12/adım). B = 499.
4. **Tam zincir kısıtlı artık blok bootstrap (k ≥ 2; k = 1 için ortak altyapıda):** test edilen sürücü
   modelden çıkarılır; kalan sürücülerin gecikmeleri gözlenen veride kendi ikili aramalarından seçilir ve
   kısıtlı model OLS ile kurulur; artıklar ortalamaya merkezlenir; **sabit uzunluklu** (hareketli) bloklarla
   yeniden örneklenir (uzunluk ∈ {12, 18, 24}/adım); hedef = kısıtlı uyum + yeniden örneklenmiş artık;
   **her yeniden örneklemede tüm gecikme seçimi ve tam modelin kurulması baştan** yapılır. İstatistik =
   |β̂_j / se_HAC|. p = (|T*| ≥ |T| + 1) / (B + 1), B = 499. Hızlandırma (QR, toplu OLS, önceden hazır
   gecikmeli matrisler) **referans uygulamayla birebir aynı sonucu** vermek zorundadır (test edilir).

Ertelenenler: tek taraflı test, takvim göstergeleri (2. aşama), STL, fixed-b HAC, AAFT/IAAFT.

## 7. Güvenilirlik kapısı

- Aday koşullar (eşikleri ayar aşamasında belirlenir): dönüşüm sonrası n; dönüşüm sonrası serilerin 1. ve
  12/adım. gecikme otokorelasyonu (mevsimsel/dengeleyici bağımlılığı kaçırmamak için); varyans kırılması
  oranı (ilk/ikinci yarı) ve ortalama kırılması işareti; ham mevsimli seride fark/dönemsel değişim
  (yalnızca "olası kontrolsüz mevsimsellik" uyarısı, yasak değil).
- **Önceden ilan edilen kullanım bölgesi R:** n ∈ {120, 250}, ayarlama ailelerinin
  {bağımsız, AR(0,5), mevsimsel(ortak bileşen yok), MA(11) çakışma}, k ∈ {1, 2, 3}, tüm sıfır/kısmi sıfır/ters
  yapılar. AR(0,9), kırılmalar, ortak bileşenli mevsimsellik, n ≤ 60, stres dönüşümleri ve görülmemiş aileler
  **bölge dışında** raporlanır (kapı bunları reddedebilir, kabul bölgesi değildir; görülmemiş aileler
  bölge-içi n ve yapılarında ayrıca kabul hücresi olarak da ölçülür, aşağıya bakın).
- Bölge içinde kapı: güvenilir işaretleme oranı ≥ %60 ve güçlü etkide (β = 0,7) güvenilir sonuçlar arasında
  güç ≥ %50 olmalı. Bunlar sağlanmazsa yöntem, kapıyı gevşeterek ya da yöntemi değiştirerek ayarda yeniden
  denenir; kapıyı ya da bölgeyi doğrulama sonrası değiştirmek yasaktır.

## 8. Aşamalar, bütçe ve kabul

1. **Ayar:** tohum kümesi S_T; hücre başına 300 tekrar. Yöntem seçme kuralı (önceden yazılı): ayar
   hücrelerinin en az %90'ında (bölge içi sıfır/kısmi sıfır/ters) güvenilir işaretli sahte alarm ≤ %5 olan
   adaylar arasından ortalama gücü en yüksek olan seçilir; eşitlikte en basit (HAC). Kapı eşikleri ve
   yöntem ayarları **dondurma kaydına** yazılır.
2. **Dondurma kaydı** (doğrulamadan önce): senaryo sayısal değerleri, etki büyüklükleri, kırılma konumları,
   seçilen yöntem ve ayarlar, kapı eşikleri, **başarısız hesapların sayımı** (istisna ve sayısal başarısızlık
   `failed` olarak ayrı raporlanır, bir hücrede > %0,1 ise o hücre araştırılır, ana oranlara dahil edilmez),
   kabul hücreleri, tekrar bütçesi, yöntem seçme kuralı.
3. **Doğrulama:** hiç kullanılmamış tohum kümesi S_V; kabul hücrelerinde (bölge içi, sıfır ve kısmi sıfır ve
   ters yapıları, ayarlama aileleri + görülmemiş aileler) hücre başına **toplam 6000 tekrar**, hücrede en az
   **1500 güvenilir işaretli tekrar** gerekir; sağlanmazsa o hücre **"kanıt yetersiz"** ve yöntem kabul
   edilmez (bütçe uzatılmaz). İç bootstrap B = 499. Sıralı durdurma yok.
4. **Kabul (kesişim–birleşim):** ayarda **tek yöntem** seçilmiş olmalıdır. Yöntem, kabul hücrelerinin
   **hepsinde** güvenilir işaretli sahte alarm için tek taraflı %95 Clopper–Pearson üst sınırı < %5 ise
   kabul edilir; hücreler arası düzeltme gerekmez (kesişim–birleşim). Doğrulama sonrası hücre ya da aday
   seçimi yasaktır. Bonferroni (hücre sayısına göre) duyarlılık raporu olarak ayrıca verilir.
5. **Doğrulama sonrası** herhangi bir ayar, eşik, bölge ya da senaryo değişirse yeni protokol sürümü,
   yeni senaryo ailesi ve yeni tohumlar gerekir; çünkü o aileden öğrenilmiştir.

## 9. Saklanacak bilgiler (her test kaydında, `details`)

Yöntem adı ve sürümü, ayarlar (maxlags, blok uzunluğu, B, kaydırma sayısı), veri kesim tarihi, seçilen
gecikmeler, güvenilirlik gerekçesi ve kapı değerleri, protokol sürümü, dondurma kaydı sürümü.

## 10. Kapsam

Ajanın başarısız bir testten sonra yeni sürücü / dönüşüm / gecikme deneyerek sonuç araması (seçim problemi
tek motor çağrısını aşar) Task 3.4'te ele alınacaktır. Güvenilmez "destekleniyor" kayıtları güvenilir
kayıtlarla eşdeğer kullanılmamalıdır (Task 3.4 tasarım maddesi).

## 11. Sürüm 1.1 eklemeleri (hiçbir ayar sonucu görülmeden yapıldı)

- **Ortak tarih kümesi (motor ve protokol uzlaştırıldı):** her pencerede, hedef ve **her sürücünün her aday
  gecikmesi** için tüm sütunların bulunduğu **en uzun kesintisiz blok** tek ortak örneklemdir. Gecikmeler aynı
  örneklemde karşılaştırılır, kenar kaybı (`lag_max` dönem) bir kez uygulanır, gözlenen test ve bütün
  yeniden örnekleme testleri aynı tarihleri kullanır. Regresyon da aynı bloğu kullanır. Minimum gözlem bu
  ortak blokta sayılır. (Motor `engine._common_block` ile böyle çalışır.)
- **Tam zincir bootstrap (aday 4) netleştirmeleri:** istatistik HAC t'si kalır ve **her yeniden örneklemede HAC
  standart hatası yeniden hesaplanır** (OLS t'ye geçmek yöntem değişikliğidir). Kısıtlı model test edilen her
  sürücü için gözlenen veride **bir kez** kurulur (kalan sürücülerin gecikmeleri gözlenenden seçilir) ve
  bütün bootstrap hedefleri bu sabit uyumdan üretilir. Üretim modeli ile üretilmiş veriye uygulanan test
  algoritması ayrıdır: her yeniden örneklemede **tüm gecikme seçimi ve tam model baştan** yapılır; gecikmeli
  sürücü sütunları sabit kalır (satır atma ya da ilk değerleri sabitleme yok). Hızlandırma özel bir
  vektörleştirilmiş HAC ile yapılır ve statsmodels çıktısıyla aynı olduğu test edilir.
- **Yeni stres senaryosu (seçim sonrası zor bölge):** ikinci sürücünün etkisi **iki bitişik gecikmeye yarı
  yarıya dağıtılmış** (gecikme 1 ve 2 puanları yakın), ayrıca sıfıra yakın katsayı (β = 0,1).
- **Yeniden örnekleme ölçeği:** kaydırma ve bootstrap `arch` (Politis–White) dışında yalnızca numpy ile.
- **Takvim:** simülasyon serileri gerçek veri bitiş ayında (2026-09) biter ve geriye doğru n gözlemlidir; n=250
  için 2. pencere (2017–2026) ≈ 117 aydır, n=120 için iki pencere aynı veridir.

## 12. Sürüm 1.2 eklemeleri (Codex kod incelemesi, 2026-10-07; hiçbir ayar sonucu görülmeden)

İlk ayar koşusu bu incelemeden sonra **iptal edildi** ve sıfırdan başlatıldı (simülatörde kırılma hatası
vardı). Düzeltmeler:

1. **Kırılmalar** ısınma sonrası tutulan pencerenin içine yerleştirilir: varyans kırılması pencerenin tam
   ortasında (sapma ×2), ortalama kırılması pencerenin orta yarısında rastgele (+1 sapma). Önceki sürüm
   kırılmayı atılan ısınma bölümünde kuruyordu.
2. **Bileşik adaylar ve uygunluk:** yalnızca k = 1 çalışan yöntemler (kaydırma, durağan bootstrap) tek başına
   motor yöntemi seçilemez. Seçilebilmek için bölge R'de k = 1, 2 ve 3 hücrelerinin hepsi olmalıdır. İkili
   yöntemler **bileşik aday** olarak değerlendirilir: k = 1 için ikili yöntem, k ≥ 2 için tam zincir bootstrap
   (`shift+chain_{12,18,24}`, `stationary_{12,18,24}+chain_{aynı}`, `stationary_pw+chain_12`). Eksik hücreler
   başarı paydasından düşürülemez.
3. **Seçim puanı:** yöntem sıralaması, bölge R'deki **bütün güç hücrelerinin** (en az 40 güvenilir tekrarlı)
   hücre başı gücünün **ortalamasıdır** (güçlü etki hücreleri yalnızca "güç ≥ %50" şartında). Yuvarlama yok;
   puanları arasındaki fark **0,01'den küçük** olanlar eşit sayılır ve en basit aileye gider (HAC, kaydırma,
   durağan, zincir).
4. **Aileler/hücreler:** t₃ ağır kuyruk ailesi (`heavy`, yalnızca istatistik katmanında) ayar ailelerine
   eklendi; R içindeki `seasonal_indep` için güç hücreleri eklendi. `heavy` bölge dışıdır.
5. **HAC ızgarası:** `hac_rule` artık gerçekten Newey-West kural bant genişliğidir (`floor(4·(n/100)^(2/9))`);
   örtüşme ufkuna dönen motor varsayılanı ayrı aday `hac_default` olarak eklendi. 11, 18, 24 sabit
   bant genişlikleri değişmedi. Gecikme düzeltmesi kapalı/açık.
6. **Sabit blok uzunlukları (12, 18, 24 ay)** artık n/3 sınırına takılmaz (yalnızca 2 ve n ile sınırlıdır);
   n/3 sınırı yalnızca Politis–White tahminine uygulanır.
7. **Sayısal başarısızlıklar:** tekil tasarım matrisi (örneğin iki özdeş sürücü) yapısal bir veri sorunudur,
   pencere `insufficient_data` olur (neden: singular regression design). Sonlu olmayan istatistik
   `NumericalFailure` fırlatır; kalibrasyonda bu çağrı `failed` olarak kaydedilir, koşu durmaz. Bir hücrede
   başarısız çağrı oranı > %0,1 ise hücre incelenir ve ana oranlara dahil edilmez.
8. **Kayıt kimliği ve izleme:** her çıktı dizininde `MANIFEST.json` (protokol sürümü, aşama, tohum, tekrar,
   parça boyutu, yöntemler, hücreler); farklı bir koşunun dizininde devam etmek reddedilir. Parça adları aşama
   ve tohumu içerir. Aynı (hücre, tekrar, yöntem) iki kez bulunursa yükleyici hata verir.
9. **Doğrulama yolu:** `study run --stage validate` yalnızca `FREEZE.json`'daki yöntemleri çalıştırır
   (filtre yok); `accept validate` kabulü verir: dondurulmuş yöntem ve kapı, tüm kabul hücrelerinin varlığı,
   hücre başına tam bütçe, ≥ 1500 güvenilir tekrar, tek taraflı %95 Clopper–Pearson üst sınırı < %5
   (kesişim–birleşim), Bonferroni duyarlılık raporu.
10. **Raporlanan ölçütlerden çıkarılan:** "katsayı güven aralığı kapsamı". Yeniden örnekleme yöntemleri
    katsayı aralığı üretmez; ölçüt yalnızca HAC için tanımlı olurdu ve seçimde kullanılmıyor. Doğru gecikme
    oranı kayıtlı gecikmelerden hesaplanır.

## 13. Sürüm 1.3: ayar sonucu ve kapsamın daraltılması (2026-10-07)

**Bu değişiklik ayar sonuçlarına bakılarak yapılmıştır.** Doğrulama tohumlarına (9001) hiç bakılmadı;
doğrulama koşusu henüz yapılmadı.

**Ayar sonucu (v1.2, 513 hücre x 200 tekrar, 25 aday):** önceden yazılan ölçütlerle **hiçbir aday geçmedi**
(rapor: `tuning-report.json`; hepsi "kapı yok"). Bu sonuç silinmez, burada kayıtlıdır.

**Neden:** bölge R'de başarısızlık neredeyse tamamen `seasonal_indep` ailesindeydi (kapısız yanlış destekleme
k1_null n=250'de %50, n=120'de %43, k2_real_null %26-33). Bu aileyi çıkarınca zincir tabanlı bütün adaylar
54 bölge hücresinin %100'ünde ≤ %5 (en kötü %2,5-4,5, ortalama ~%0,5); HAC çeşitleri %78-96. Kapı
(gecikme-12 otokorelasyonu) mevsimsel aileleri ayırıyor: medyan 0,47 (en küçük 0,31); iid, ar05, heavy,
var_break ≤ 0,34; ma11 (en çok 0,68) ve ar09 (0,67) ile örtüşme var.

**Yorum (Codex incelemesiyle düzeltildi):** `seasonal_indep` bir "tanım hatası" değildir. Fazları bağımsız
çekilen sinüsler olasılık anlamında bağımsız süreçlerdir; tek gerçekleşmede görülen yüksek ve kalıcı korelasyon
bağımsızlıkla çelişmez. Sorun, sönmeyen periyodik bağımlılığın yöntem varsayımlarını (azalan bağımlılık)
bozmasıdır. Bu nedenle bu aileyi sessizce silmiyoruz: **destek verilen kapsamı daraltıyoruz** ve ham
mevsimli seriler için "güvenilir sonuç yok" sonucunu üretimde zorunlu kılıyoruz.

**Değişiklikler:**
1. **Bölge R** = {iid, ar05, ma11, **sar12_05**} x n {120, 250} x tüm yapılar (k = 1, 2, 3). `seasonal_indep`
   ve `seasonal_common` R'den çıktı.
2. **"Kapının reddetmesi gereken" aileler:** `seasonal_indep`, `seasonal_common`, **`seasonal_weak`** (genlik
   0,4), **`sar12_08`** (stokastik mevsimsel AR, lag 12, φ = 0,8), n {120, 250}, tüm yanlış-destekleme yapıları.
   Kriter (ayarda nokta tahmini ile ≥ %90 hücrede, doğrulamada **her** hücrede Clopper–Pearson ile):
   (i) **sızıntı** = P(güvenilir ve yanlış destekleme) **tüm tekrarlar üzerinden**, üst sınır < %2,5;
   (ii) **güvenilir kabul payı** P(güvenilir) tüm tekrarlar üzerinden, üst sınır < %10. Bu hücrelerde
   asgari güvenilir tekrar aranmaz. Eşikler (%2,5 ve %10) ürün/koruma tercihidir, matematiksel zorunluluk değil.
3. **Yeni aileler** (ayarda, bu karardan önce sonuçları bilinmeden sınıflandırıldı): `seasonal_weak` ve `sar12_08`
   reddedilmesi gerekenler; **`sar12_05`** (φ = 0,5) bölge içi kabul ailesidir. Ayar koşusu bu üç aile için ek
   dizinde (`tune-v13-extra`) çalıştırılır; aynı tohum alanı ve protokol.
4. **Kapı ızgarası:** `rho_seasonal_max` değerlerine 0,3 ve 0,35 eklendi (mevsimsel ailelerin en küçük gözlenen
   değeri 0,31).
5. **Doğrulama hücreleri:** {iid, ar05, ma11, sar12_05} (kabul) + {seasonal_indep, seasonal_common, seasonal_weak,
   sar12_08} (reddedilmeli) + görülmemiş {arma, ar_neg, hetero, semisynth} (kabul) x n {120, 250} x 9 yanlış-
   destekleme yapısı = **216 hücre**. Diğer aileler (ar09, kırılmalar, heavy) ve n ≤ 60 yalnız raporlanır.
   Bütçe (hücre başına tekrar) seçilen yöntem belli olunca, **doğrulamadan önce** FREEZE kaydında kilitlenir
   (≤ 6000; kabul hücrelerinde ≥ 1500 güvenilir tekrar şartı korunur; bütçe sonuca bakılarak uzatılmaz).
6. **Üretim şartı:** ham (mevsimden arındırılmamış) seride sonuç, kapı reddettiği sürece "güvenilir" sayılmaz.
   "Mevsimselliği destekliyoruz" iddiası yapılmaz; desteklenen kapsam bölge R'dir.

Eski kapsamın (R'de `seasonal_indep` dahil) başarısızlığı bu sürümde silinmemiş, korunmuştur.

## 14. Sürüm 1.4: ikinci ayar sonrası revizyon (2026-10-07)

**Bu da ayar sonuçlarına bakılarak yapılmış bir revizyondur** (v1.2 ve v1.3 başarısızlıkları silinmez).
Doğrulama tohumlarına (9001) hâlâ hiç bakılmadı. Codex incelemesinin uyarısı geçerlidir: ardışık
sınıflandırma değişiklikleri ayar verisine uyum anlamına gelir; bu sürüm **son** revizyondur, doğrulama
başarısız olursa aynı doğrulama ailesine göre yeniden ayar yapıp "bağımsız doğrulama" denmez.

**v1.3'ün ayar sonucu:** yeni üç aileyle de hiçbir aday geçmedi. Teşhis (kapısız, n in {120, 250}):
1. k = 1'de **dairesel kaydırma tüm ailelerde düşük hata gösterdi** (toplu yanlış destekleme: iid %0,2,
   ar05 %0,9, ma11 %0,8, sar12_05 %0,6, sar12_08 %1,2, seasonal_indep %0,9, seasonal_common %3,2,
   seasonal_weak %0,9; mean_break %3,5, en kötü hücre %8,5). Gücü zincir bootstrap ile benzer (güçlü etkide
   0,909 / 0,912). Bu, "kaydırma mevsimsellikte geçerlidir" demek değildir; yalnızca **bu ayar hücrelerinde
   düşük hata gözlendi**. Mekanizma yalnız 12'nin katları değil, kaydırmaların oluşturduğu göreli faz
   dağılımıdır; uçların birleştirilmesi ve guard sonuca etki eder. Bu yüzden doğrulamada farklı genlik/gürültü
   oranları, kalıcılıklar ve 12'nin katı olmayan örneklem uzunlukları ek kontrol olarak vardır.
2. k >= 2'de zincir bootstrap kalıcı mevsimsellikte bozuluyor (seasonal_indep %18, seasonal_common %67,
   sar12_08 %8, mean_break %6); diğer ailelerde ≤ %2,3. Kapı kalıcı mevsimsel pencereleri reddetmeli.
3. Kapının tek mevsimsel özelliği (gecikme-12 otokorelasyonu) `sar12_05` (0,49), `seasonal_indep` (0,47) ve
   hatta `ma11`'in üst kuyruğunu (en çok 0,68) ayıramıyordu. Kriterlerin birlikte sağlanması bu yüzden
   imkânsızdı (güvenilir oran ≥ %60 ile mevsimsel ailelerin reddi).

**Değişiklikler (doğrulamadan önce, tamamı bu belgede):**
1. **Yeni kapı özelliği: gecikme-24 otokorelasyonu** (`rho_seasonal24`). Kalıcı mevsimsellik = lag-12 VE
   lag-24 otokorelasyonu **birlikte** yüksek (deterministik döngü lag 24'te de otokorelasyonunu korur, yıllık
   değişim (MA(11)) ve stokastik mevsimsel AR(12) φ = 0,5 kaybeder). Eşik `rho_seasonal24_max` ayarda
   belirlenir; `-1` lag-24 koşulunu kapatır. Özellik eski ayar kayıtlarına, **aynı hücreler ve tohumlarla**
   deterministik yeniden hesaplanarak eklenir (`tune-v14-features`); ortak özelliklerin birebir eşitliği
   otomatik denetlenir. Kapı, bu özellikle **baştan** ayarlanır.
2. **Bölge R** = {iid, ar05, ma11, sar12_05, **seasonal_weak**} x n {120, 250} x tüm yapılar (kabul hücreleri).
   `seasonal_weak` R'ye geçti: gerekçe "kapı ayıramıyor" değil, bu kullanım alanını desteklemek istememiz ve
   aynı koşullu CP sınırıyla doğrulayacak olmamızdır (ayarda en kötü hücre %6; doğrulamada sınırı aşarsa
   yöntem kabul edilmez).
3. **Kapının reddetmesi gerekenler** = {seasonal_indep, seasonal_common, sar12_08} + ayarda hiç görülmemiş iki
   kapı-sınırı kontrolü {**seasonal_mid** (genlik 0,6 / gürültü 0,7), **sar12_065** (φ = 0,65)}. Kriterler
   değişmez (sızıntı CP üst < %2,5, güvenilir kabul payı CP üst < %10). Bu kriterler tam reddetme garantisi
   veya koşullu %5 güvencesiyle eşdeğer değildir; yalnızca bu ürün tercihidir.
4. **Doğrulama hücreleri** = 14 aile x n {120, 250} x 9 yapı (252) + 12'nin katı olmayan uzunluk kontrolleri
   (iid, ma11, seasonal_indep x n {131, 245} x 9 yapı = 54) = **306 hücre**. Tümü nihai iki-pencere kararı ve
   iki pencerenin de kapıyı geçmesi üzerinden ölçülür.
5. **Bileşik yönlendirme sabit:** k = 1 için ikili yöntem, k >= 2 için tam zincir bootstrap; ayardan önce
   yazıldığı gibi `k` üzerinden.
6. **Doğrulama kısayolu (kesin):** kapı yalnız veri özelliklerine baktığından, bir pencereyi geçemeyen tekrar
   yöntem ne derse desin güvenilmezdir; doğrulama koşusu bu tekrarlarda yöntemi çalıştırmaz ve "güvenilmez"
   işaretler (`skipped_by_gate`, sonuç alanı kural olarak `I`).
7. **Kapsam iddiası:** `sar12_05` ve `seasonal_weak` bölgededir (kapı onları reddetmez); kalıcı döngüsel
   mevsimsellikte (lag-12 ve lag-24 yüksek) hiçbir "güvenilir destek" sonucu üretilmez. `mean_break` için
   kapının ortalama kırılma eşiği ayarda seçilir; kırılma büyüklüğü/konumu için doğrulamada ayrıca rapor
   edilir, iddia olarak "kırılmaya karşı güvence" verilmez.
8. **Bütçe:** doğrulama hücre başına 3000 tekrar (≤ 6000 kuralı içinde), kabul hücrelerinde ≥ 1500 güvenilir
   tekrar şartı korunur; FREEZE'de kilitlenir ve sonuca bakılarak uzatılmaz.

## 15. Sürüm 1.5: ayar-2 aşamasının ön-kaydı (2026-10-07, ayar-2 sonuçları görülmeden yazıldı)

v1.4 ayarında (200 tekrar) kapı çalıştı ve çok sayıda bileşik aday ölçütleri geçti, ancak: (i) ayar ölçütü
("hücrelerin ≥ %90'ı") doğrulama ölçütünden ("her hücre CP < %5") çok gevşektir; (ii) 200 tekrar tek hücrede
%5 / %7 ayrımını yapamaz; (iii) üretimde 2017–2026 penceresi aylık veride **117 gözlemdir** ve doğrulanan en
küçük boyun altına düşerse sonuç hiç güvenilir olamaz. Codex incelemesi bu üçünü ve "bilinen kötü hücrelerin
ortalamada kaybolması" riskini gösterdi (`seasonal_common` güvenilir pay %14, sızıntı %6,5). **Ayar-2** bu
nedenle yapılır; aşağıdaki kurallar ayar-2 verisi görülmeden kilitlenmiştir.

**Ayar-2 verisi:** yalnız bölge + reddedilecek aileler (iid, ar05, ma11, sar12_05, seasonal_weak, seasonal_indep,
seasonal_common, sar12_08) x 9 yanlış-destekleme yapısı + bölge ailelerinin 8 güç yapısı; **n ∈ {100, 250}**;
n = 100 için tekrar 0-799, n = 250 için tekrar 200-599 (ayar tohum alanı 1001; doğrulama tohumları dokunulmadı).
Yöntemler: shift, stationary_pw, chain_12, chain_24 ve bunlardan kurulan bileşikler (shift+chain_12,
shift+chain_24, stationary_pw+chain_12) ile k >= 2 için chain_12 ve chain_24 tek başına.

**Boyut kapsamı:** bölge R ve doğrulama **n ∈ {100, 250}**; kapının alt sınırı **n_min = 100 (sabit, ayarlanmaz)**:
100'ün altındaki pencere "güvenilmez"dir. (Üretimdeki 2017–2026 penceresi aylık veride 117'dir.) Bundan
daha kısa pencerelerde güvenilir sonuç iddiası yoktur. Mod-12 kontrolleri n ∈ {111, 245} (kaydırılan uzunluk
n + lag_max − lag_min olduğundan mod 6 ve 8).

**Uygunluk filtresi (yöntem seçiminden ÖNCE; ayar-2 verisinde, nihai iki-pencere kararı ve iki pencerenin de
kapıyı geçmesi üzerinden):** bir (yöntem, kapı) çifti uygun olmak için aşağıdakilerin **hepsini** sağlamalı:
1. her bölge yanlış-destekleme hücresinde ≥ 200 güvenilir tekrar ve koşullu yanlış destekleme oranı ≤ **%4,0**
   (tavandan 1 puan güvenlik payı); ≥ 200 güvenilir tekrarı olmayan hücre "kanıt yetersiz" sayılır ve çifti
   eler;
2. her "reddedilmeli" hücresinde sızıntı (güvenilir ve yanlış destekleyen / tüm tekrarlar) ≤ **%1,5** ve güvenilir
   kabul payı ≤ **%8**;
3. bölgede güvenilir işaretleme oranı ≥ %60 ve güçlü etkide (β = 0,7) güvenilir sonuçlar arasında güç ≥ %50.
Uygun çiftler arasında sıralama: bölge güç hücrelerinin (≥ 200 güvenilir tekrarlı) ortalama gücü; 0,01 içinde
eşitlikte en basit aile (HAC, shift, stationary, chain). **Uygun çift yoksa sonuç "dondurulacak aday yok"tur**
ve doğrulama yapılmaz.

**Tek ön-kayıtlı geri çekilme:** hiçbir (yöntem, kapı) çifti uygun değilse ve yalnızca `sar12_05` hücreleri filtre
1'i bozuyorsa, `sar12_05` "reddedilmeli" sınıfına taşınır (desteklenen kapsam kalıcı mevsimsellik ve φ ≥ 0,5
stokastik mevsimselliği dışlar) ve filtre yeniden uygulanır; yine uygun çift çıkmazsa "dondurulacak aday yok".
`seasonal_weak` ve diğer bölge aileleri için geri çekilme yoktur.

**Doğrulama bütçesi:** hücre başına **4000 tekrar** (nihai iki-pencere kararlarının güvenilir olduğu tekrar
sayısı ≥ 1500 şartı korunur; sağlanamayan hücre "kanıt yetersiz" ve yöntem kabul edilmez; bütçe uzatılmaz).
Düşük güvenilirlik (ör. ma11 güvenilir pay) ayrı bir **ürün riski**dir, rapor edilir, daha çok simülasyonla
düzelmez. FREEZE'e hücre manifestinde `sar12_05` sınıfı (bölge/reddedilmeli) ve ayar-1'den ayar-2'ye değişikliği
açıkça yazılır.

## 16. Ayar-2 sonucu ve ön-kayıt eratumu (2026-10-07)

**Sonuç (ön-kaydın sonuç kuralı):** hiçbir (yöntem, kapı) çifti §15 uygunluk filtresini sağlamadı; ön-kayıtlı
tek geri çekilme (`sar12_05` → reddedilmeli) da yetmedi. **Dondurulacak aday yok; doğrulama koşulmadı;
doğrulama tohumları (9001) açılmadı.** Ayrıntı ve ölçülen hata profili: `RESULT.md`, `RESULT-tables.md`,
`tuning-report-v15.json`.

**Erratum (§15'te ön-kayıt hatası):** k = 3 regresyon aylıkta 36 × 3 = 108 gözlem ister; n = 100'de her tekrar
yapısal olarak `insufficient_data`dır. "Her bölge hücresinde ≥ 200 güvenilir tekrar" şartı bu 15 hücrede
(k3_real_null_null, k3_real_real_null, k3_real_real_real x 5 aile) veriden bağımsız olarak sağlanamazdı.
Bu hata ön-kayıt sırasında görülmedi. **Sonuç değiştirilmedi:** hücreler dışarıda bırakılsa da uygun çift
yoktur (seasonal_weak k = 1 n = 100 tüm adaylarda %4,9-5,6, ma11 k = 1 n = 100 shift/stationary'de %5,1-5,2,
`sar12_05` hücreleri). Açıklayıcı yeniden çözümleme özgün sonucun yerine geçmez.

**Teslim kararı (Codex ile):** motor "deneysel analiz motoru" olarak teslim edilir (`RESULT.md`); varsayılan yöntem
ayar sonuçlarına bakılarak seçilmiş deneysel `WholeChainBootstrap(24, 499)`, `calibrated=False`; kapı yalnız
bilgilendirir; `reliable` her zaman `False`; doğrulama, kapı ve yapısal uygunluk ayrı alanlarda tutulur.
