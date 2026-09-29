# Karven — Gereksinimler (yeni proje, MVP)

> Bu doküman Karven MVP'sinin **ne yapması gerektiğini** fonksiyonel (FR) ve
> fonksiyonel olmayan (NFR) gereksinimler olarak listeler. Her gereksinim
> `docs/DECISIONS.md`'deki bir karara dayanır; köşeli parantez içindeki
> § numarası o karara işaret eder. Karar ile bu belge çelişirse
> **`DECISIONS.md` geçerlidir.** Teknik yapı için
> `docs/PRODUCTION-ARCHITECTURE.md`, iş sırası için `roadmap/ROADMAP.md`.
>
> Eski projenin gereksinim belgesi (iki yollu mimari, iddia doğrulama,
> denetleyici, mekanizmacı/şüpheci/hakem) 2026-09-29'da yeniden başlamayla
> geçersiz oldu; git geçmişinde durur.

## 1. Genel bakış

Karven, Türkçe ekonomi haberlerini okur; **(1) haberde anlatılanları resmî
veriyle görselleştirir** ve **(2) haberden ekonomik ilişki çıkarıp resmî
veriyle test eder.** Sonuçlar admin onayından sonra herkese açık bir web
sitesinde yayımlanır. Ana hedef ilişkilerdir, iddia doğrulama değildir. [§2]

## 2. MVP başarı kriteri

MVP, uçtan uca çalışan bir senaryoyla tamamlanmış sayılır: bir ekonomi haberi
RSS'ten gelir; birinci ajan haber görsellerini ve ilişki fikirlerini çıkarır;
ilişkiler knowledge graph ajanında resmî TÜİK/TCMB verisiyle test edilip
Neo4j'ye yazılır; grafikler çizilir, kodla kontrol edilir, admin onaylar ve
haber sitede görünür. Bu akış **gerçek veriyle ve canlıda** kanıtlanmalıdır
(Faz 5 ana milestone). [§3, §13]

## 3. Kapsam

**MVP'de var:**
- Herkese açık, girişsiz web sitesi; tek admin için giriş. [§11]
- 5 RSS kaynağından her haber (ekonomik değil diye eleme yok). [§2, §6]
- Veri kaynakları: TÜİK ve TCMB. [§5]
- 4 ajan: birinci ajan, knowledge graph ajanı, görselleştirme ajanı, veri
  çekme ajanı. [§4]

**MVP'de yok:** sayısal iddia doğrulama, tekrar haber kontrolü, CSV indirme,
otomatik yayın, otomatik sosyal medya paylaşımı, admin panelinde graf
görünümü, bütçe sınırı, CI. Sonrası için liste: `roadmap/post-mvp.md`.
[§2, §4.5, §9, §11, §12]

## 4. Fonksiyonel gereksinimler

### Haberler

**FR-1 — Haber toplama.** Sistem 5 RSS kaynağını (Sabah ekonomi, Habertürk
ekonomi, Sözcü ekonomi, BloombergHT, CNN Türk finans) **15 dakikada bir**
tarar ve haber sayfasındaki **tam metni** çeker. Her haber işlenir. [§6]

### Ajanlar ve akış

**FR-2 — Birinci ajan.** Haberin tam metnini okur ve iki liste çıkarır:
**(a) haber görselleri** (haberde anlatılanın kendisi; haberde geçen rakam ve
dönem de çıkarılır) ve **(b) ilişki fikirleri.** Haber başına en fazla 8
ilişki fikri, 8 görsel adayı ve 60 araç çağrısı. Araçları: kavram ağacı,
katalog arama, veri durumu, graf okuma, graph ajanına sorma, veri çekme
ajanıyla doğrudan konuşma. Sonuçlar gelince hangi grafiklerin
görselleştirileceğine karar verir. [§4.1]

**FR-3 — Haber görselleri test edilmez.** (a) listesi graph ajanına gitmez,
doğrudan görselleştirmeye gider. Haberdeki rakam grafikte "haberde belirtilen"
notuyla gösterilir; resmî veriyle karşılaştırılmaz, hüküm verilmez. [§3, §4.1]

**FR-4 — Knowledge graph ajanı.** Tek ajandır. Gelen fikir grafta varsa var
olan sonucu kullanır ve haberi o ilişkiye bağlar. Yoksa **veriyi görmeden**
hipotez yazar (mekanizma, beklenen yön, gecikme aralığı, seriler, dönüşüm,
enflasyondan arındırılacak seriler); testi kod yapar; sonuç grafa yazılır ve
gerekçesiyle birinci ajana döner. İlişkiler 2 veya daha fazla seri arasında
olabilir. [§4.2, §8]

**FR-5 — Görselleştirme ajanı.** Her grafik için Vega-Lite tarifi yazar;
sayılara dokunmaz, çizimi kod yapar. Kod kontrolünden dönen geri bildirimle
düzeltir. [§4.3, §10]

**FR-6 — Veri çekme ajanı (karma yapı).** Ajan talebi çekme planına çevirir,
hatada teşhis koyar, yeniden dener ya da rapor yazar. Kod izler, durumu
değiştirir ve bekler (`istendi → çekiliyor → tamamlandı / hata`). "Yaşıyorum"
sinyali sürdükçe beklenir, sabit süre sınırı yoktur; sinyal kesilirse iş
takılmış sayılır. Veri beklenirken ilgili fikir/görsel park edilir, akış
devam eder; veri gelince kaldığı yerden sürer. [§4.4]

**FR-7 — Ajanlar değer görmez.** Hiçbir ajan serinin sayısal değerlerini
görmez; yalnızca meta bilgi (var mı, hangi yıllar, frekans, birim, uygun
dönüşüm) görür. [§4]

### Veri

**FR-8 — Resmî veri.** TÜİK ve TCMB verisi sıfırdan çekilir. Çekirdek seriler
önceden, diğerleri talep üzerine; ikisi de **2000-01-01'den itibaren.** Talep
üzerine çekilen seri otomatik çekirdeğe alınmaz. Çekirdek liste Faz 1'de
belirlenir. [§5]

**FR-9 — Çekilme zamanı ve revizyonlar.** Her değerin ne zaman çekildiği en
baştan kaydedilir. Kurum geçmiş bir değeri düzeltirse düzeltme yeni kayıt
olur, eski değer silinmez; test ve grafikler son değeri kullanır. [§5]

**FR-10 — Ham cevaplar.** TÜİK/TCMB'den gelen ham cevaplar MinIO'da saklanır.
[§12]

**FR-10b — Elle girilen göstergeler.** TÜİK/TCMB'de olmayan göstergeler (ör.
asgari ücret) elle güncellenen bir tabloda tutulur; hangileri ve nasıl
girileceği Faz 1'de kararlaştırılır. [§5]

**FR-10c — Veri kesilme uyarısı.** Bir kaynaktan veri sessizce gelmemeye
başlarsa ya da cevabın biçimi değişirse admin panelinde uyarı görünür; kuralı
Faz 1'de kararlaştırılır. [§5]

### Katalog

**FR-11 — Etiketleme.** Seriler kapalı bir etiket listesinden (kavram ağacı)
yalnızca meta bilgiyle Jev tarafından etiketlenir. Güven eşiği üstü otomatik
kabul, altı Claude tarafından incelenir; eşik ~200 serilik kalibrasyonla
belirlenir. Okuyucu için tek cümlelik açıklama tutulur, aramada kullanılmaz.
[§7]

**FR-12 — Katalog araması (embedding yok).** Ajan isteği düz dille yazar →
Jev her etiketi 1-5 puanlar, 4-5 geçer → kod bu etiketlerden herhangi birini
taşıyan serileri bulur → Jev serileri 1-5 puanlar (gerekirse parça parça) →
4-5 alanlar (ad + meta, değer yok) ajana gider. Hiçbiri yoksa "güçlü eşleşme
yok" döner; ajan en fazla 3-4 kez yeniden arayabilir. Kalite 30-40 sorguluk
test setiyle ölçülür. [§7]

### İlişki testi

**FR-13 — Sade test.** Kod korelasyon + gecikme araması yapar; 3 ve daha
fazla üyeli ilişkilerde regresyon. Eldeki tüm veri kullanılır (haber
tarihinden bağımsız). İki dönem: bütün yıllar ve 2017–2026. [§8]

**FR-14 — Test kuralları.** En az gözlem aylık 36, üç aylık 12, yıllık 10;
regresyonda frekans eşiği × açıklayıcı sayısı. Ham seviye test edilmez;
dönüşüm (yıllık %, dönemsel %, fark) hipotezde seçilir. Frekans düşüğe
indirilir (fiyat/oran/endeks ortalama, akım toplam). Nominal TL tutarları
TÜFE ile reelleştirilir. [§8]

**FR-15 — Test sonucu.** Her dönem: destekleniyor / desteklenmiyor / veri
yetersiz. İlişki durumu: ikisi destekliyorsa destekleniyor, ikisi de
desteklemiyorsa desteklenmiyor, farklıysa zamanla değişmiş. Sonuç okuyucuya
gösterilmez, admin ekranında görünür. [§8]

**FR-16 — Yeniden test.** Aynı ilişki yeni haberle tekrar gelirse ve son sonuç
"veri yetersiz" ya da 30 günden eskiyse, aynı hipotezle yeniden test edilir;
yeni test ayrı kayıttır. Periyodik toplu yeniden test yoktur. [§8]

### Graf

**FR-17 — İlişki grafı.** Neo4j başta boştur, her haberle büyür. İlişki kendi
düğümüdür, 2+ seriye ve kaynak haberlerine bağlanır. Her test ayrı kayıttır.
Aynı ilişki = aynı seri kümesi + aynı roller; ters yön ayrı ilişkidir.
Reddedilen ilişkiler silinmez. [§9]

### Görselleştirme

**FR-18 — Grafikler.** Türler: çizgi, endeksli çizgi (çift eksen yok),
gecikmeli dağılım, çubuk. Dönem kapalı listeden seçilir (son 2 yıl, son 5
yıl, 2017'den beri, 2000'den beri); okuyucu sitede değiştirebilir. Haber
başına en fazla 5 haber görseli + 5 ilişki grafiği; fazlası saklanır. Her
grafik için tarif, veri sürümleri ve kaynak notu saklanır. [§10]

**FR-19 — Kod kontrolü.** Başarısız grafik geri bildirimle görselleştirme
ajanına döner, en fazla 2 tur; hâlâ başarısızsa yayımlanmaz, admin
incelemesi için saklanır. [§10]

**FR-20 — Yayın anında çizim.** Grafik yayın anında en güncel veriyle çizilir;
kaynak notunda verinin son dönemi yazar. Sosyal medya için PNG üretilir. [§10,
§11]

### Site ve admin

**FR-21 — Haber sayfası.** Başlık ve kaynağa link, interaktif grafikler, veri
tablosu, kaynak notu. Hiç grafiği olmayan haber yayımlanmaz. [§11]

**FR-22 — Admin onayı.** Her yayın admin onayından geçer; ilişki
grafiklerinden hangilerinin yayımlanacağına admin karar verir. Bildirim yok;
bekleyen sayısı panelde görünür. Paylaşım elle yapılır. [§11]

**FR-23 — Admin listeleri.** Panelde: onay bekleyen haberler, çekilemeyen
veriler (hangi veri, neden, ajanın önerisi), başarısız haberler (elle yeniden
başlatma). [§4.4, §4.6, §11]

**FR-23b — Geri çekme ve izleme.** Admin yayımlanmış haberi siteden
kaldırabilir. Panelde veri kaynaklarının durumu (çalışıyor mu, son çekme
zamanı, hata) ve haberlerin hangi aşamada olduğu görünür. [§11]

**FR-23c — Prompt yönetimi.** Prompt'lar veritabanında sürümlü tutulur; admin
panelinden kod değişikliği gerekmeden yeni sürüm eklenip etkinleştirilir. [§4]

**FR-24 — Admin girişi.** Tek admin, kullanıcı adı + şifre (hash `.env`'de),
oturum çerezi. [§11]

## 5. Fonksiyonel olmayan gereksinimler

**NFR-1 — LLM.** Dört ajan DeepSeek v4.1 Flash; sağlayıcı EVREN, yedek
OpenRouter. Jev önce TypeSafe, kredi bitince OpenRouter. [§4, §7]

**NFR-2 — Maliyet.** Bütçe sınırı yok; her haberin LLM maliyeti kaydedilir.
[§4.5]

**NFR-3 — Hata davranışı.** Hatalı fikir yalnızca kendisi düşer. Yarıda kalan
haber otomatik yeniden denenmez, başarısız haberler listesine düşer. Haber
başına süre sınırı yok; ne veri bekleyen ne de "yaşıyorum" sinyali veren haber
takılmış sayılır. [§4.6]

**NFR-4 — LLM kaydı.** Her LLM çağrısının ham isteği ve cevabı MinIO'da,
anahtarlar maskelenerek saklanır. [§12]

**NFR-5 — Hacim.** Günde yüzlerce haber olabilir; onay yükü artınca yayın
otomatikleştirilir (MVP sonrası). [§11]

**NFR-6 — Adlandırma.** Tablo/sütun, dosya ve kod adları İngilizce; belge
içerikleri Türkçe. [§12]

**NFR-7 — Ortam.** MVP bu bilgisayarda çalışır, site dışarı açılmaz. Test
ortamı canlıyla port, imaj etiketi ve veritabanı paylaşmaz. [§12, §13]

**NFR-8 — LLM dayanıklılığı.** Araç turlarında JSON şeması yalnızca son turda
gönderilir; sızan şablon belirteçleri (`<|im_end|>` vb.) temizlenir; 429'da
uzun ve artan bekleme. [§13]
