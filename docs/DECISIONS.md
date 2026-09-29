# Karven — Kararlar (yeni proje, MVP)

> Bu doküman yeni Karven projesinin kullanıcıyla birlikte alınmış kararlarını
> tek yerde toplar. Eski projeden (2026-09-04 – 2026-09-29) kod ve veri
> taşınmaz; burada yalnızca yeni proje için geçerli kararlar vardır. Roadmap
> (`roadmap/ROADMAP.md`) "hangi sırayla", bu doküman "neye karar verdik"
> sorusunu cevaplar. Bir karar değişirse burada güncellenir ve tarihi yazılır.
> Bazı konular bilinçli olarak proje ilerlerken ilgili fazda kararlaştırılacaktır;
> bunlar en sondaki **Proje içinde verilecek kararlar** bölümündedir.
>
> Kararların tamamı 2026-09-29 tarihli konuşmada alınmıştır.

## 1. Yeniden başlama

- Proje sıfırdan başlar: **kod, veri, GitHub reposu ve Docker kurulumu
  yenidir.** Eski projeden yalnızca roadmap ve karar dokümanları (güncellenerek)
  ile TÜİK/TCMB kaynak profilleri (`docs/source-profiles/`) alınır.
- Eski projenin kodu, veritabanı ve canlı ortamı yeni projeye bağlanmaz.
- Eski GitHub reposunu (`alpbel0/karven`) **kullanıcı siler**; yeni proje
  klasörünü **kullanıcı açar.** Eski `docs/` altındaki diğer belgeler (teknik
  tasarım, eski planlar, incelemeler, raporlar) yeni projeye taşınmaz.
- **API anahtarları eski `.env`'den taşınmaz;** yeni `.env`'i kullanıcı
  doldurur.

### Proje adı

- Proje adı **Karven**'dir (kesin; eskiden "geçici isim" olarak geçiyordu).
- Anlamı: **Kar** — Eski Asur döneminin Anadolu'daki ticaret merkezleri için
  kullanılan *kārum* kelimesinden; **Ve** — Türkçe *veri* kelimesinden;
  **N** — söyleyişi tamamlayan ses, ayrı anlamı yok. Amaç, geçmişten gelen bir
  kökle bugünün verisini birleştirmektir.
- Adın anlamı `docs/PROJECT-OVERVIEW.md`'de anlatılır; sitedeki "Hakkında"
  metninde kullanılacak tarihî bilgiler yayından önce kaynağından doğrulanır.

## 2. Ürün kapsamı (MVP)

- Karven, Türkçe ekonomi haberlerini okuyup **(1) haberde anlatılanları resmî
  veriyle görselleştiren** ve **(2) haberden ekonomik ilişki çıkarıp resmî
  veriyle test eden** bir sistemdir. Sonuçlar herkese açık bir web sitesinde
  yayımlanır.
- Ana hedef **ilişkilerdir**, iddia doğrulama değil. Her haberden bir şey
  çıkabilir (ör. bir deprem haberinden depreme ilişkin ekonomik veri
  düşünülebilir); haber "ekonomik değil" diye elenmez.
- **MVP'de yok:** sayısal iddia doğrulama, tekrar haber kontrolü (Jev ile son
  48 saat karşılaştırması), CSV indirme.

## 3. Akış

```
Haber (RSS, tam metin)
  → Birinci ajan ──(a) haber görselleri──────────────────────────┐
        │  (b) ilişki fikirleri                                  │
        ↓                                                        │
    Knowledge graph ajanı (hipotez → kodla test → grafa yaz)     │
        │                                                        │
        ↓                                                        ↓
  Birinci ajan son listeyi yapar → Görselleştirme ajanı → Kod kontrolü
  → Admin onayı → Site
Yan kol: gerektiğinde Veri çekme ajanı (birinci ajan ve graph ajanı çağırabilir)
```

- (a) listesi test edilmez ve graph ajanına gitmez; doğrudan görselleştirmeye
  gider. (b) listesi graph ajanında test edilir.
- Her iki listenin grafikleri de görselleştirme ajanından geçer.

## 4. Ajanlar

- **Toplam 4 ajan:** birinci ajan, knowledge graph ajanı, görselleştirme ajanı,
  veri çekme ajanı. Grafik değerlendirmesi ajan değil, koddur.
- **Model:** dört ajan da **DeepSeek v4.1 Flash**; sağlayıcı **EVREN**, yedek
  **OpenRouter**.
- **Ajanlar hiçbir zaman değer (sayı) görmez.** Meta bilgi görebilir: verinin
  var olup olmadığı, hangi yıllar arası, frekansı, birimi, uygun dönüşüm.
- **Prompt'lar veritabanında sürümlü** tutulur; eski sürümler silinmez. Admin
  panelinden, kod değişikliği gerekmeden yeni sürüm eklenip etkinleştirilebilir.
- Ajan izin modeli (okuma / öneri / yazma yetkileri) ve araçların MCP
  üzerinden sunulması **MVP sonrasında** yapılır (`roadmap/post-mvp.md`).

### 4.1 Birinci ajan

- Haberin **tam metnini** okur.
- İki liste çıkarır:
  - **(a) Haber görselleri:** haberde anlatılanın kendisi (ör. benzin zammı
    haberi → aylık benzin fiyatı). Haberde geçen rakam ve dönemi de çıkarılır;
    grafikte "haberde belirtilen" notu olarak gösterilir, resmî veriyle
    karşılaştırılmaz, hüküm verilmez.
  - **(b) İlişki fikirleri:** haberde geçen ya da haberden düşünülebilecek
    ekonomik ilişkiler (ör. emekli maaşı haberi → emekli maaşı ile asgari ücret).
- Sınırlar (haber başına): **en fazla 8 ilişki fikri, 8 görsel adayı, 60 araç
  çağrısı.**
- Araçları: kavram ağacı, katalog arama, veri durumu, graf okuma, graph ajanına
  sorma, **veri çekme ajanıyla doğrudan konuşma** (ör. "haber Eylül enflasyonu
  diyor, bende Ağustos var").
- Son listede hangi grafiklerin görselleştirileceğine karar verir.

### 4.2 Knowledge graph ajanı

- Tek ajandır (eski projedeki mekanizmacı/şüpheci/hakem üçlüsünün yerine).
- Bir fikir gelince önce **grafta var mı** bakar; varsa var olan sonucu kullanır
  ve yeni haberi o ilişkiye bağlar.
- Yoksa **önce hipotez** yazar — veriyi görmeden: mekanizma, beklenen yön,
  gecikme aralığı, seriler. Sonra testi **kod** yapar, sonuç grafa yazılır ve
  birinci ajana gerekçesiyle döner.
- İlişkiler **2 veya daha fazla seri** arasında olabilir.

### 4.3 Görselleştirme ajanı

- Grafiğin tarifini (**Vega-Lite**) yazar; sayılara dokunmaz, çizimi kod yapar.
- Tek başına değerlendirme yapmaz; kod kontrolünden dönen geri bildirimle
  düzeltir.

### 4.4 Veri çekme ajanı (karma yapı)

- **Ajanın işi:** talebi doğru çekme planına çevirmek (hangi tablo, hangi
  parametre); hata olursa teşhis koymak, yeniden denemek ya da rapor yazmak.
- **Kodun işi:** izleme, durum değişikliği ve bekleme. Durumlar:
  `istendi → çekiliyor → tamamlandı / hata`. Çekme işi "yaşıyorum" sinyali
  verdiği sürece beklenir; **sabit 20 dakika sınırı yok.** Sinyal kesilirse iş
  takılmış sayılır.
- Veri gelene kadar ilgili fikir ya da görsel **park edilir**, akış diğer
  işlere devam eder; veri gelince kaldığı yerden sürer.
- Çekilemeyen veriler **admin panelinde liste** olarak görünür (hangi veri,
  neden, ajanın önerisi).

### 4.5 Maliyet

- Şimdilik **bütçe sınırı yok**; EVREN ile devam edilir. Her haberin LLM
  maliyeti kaydedilir. EVREN dönemi bitince (ör. haber başına fikir sayısını
  azaltmak gibi) önlemler konuşulur.

### 4.6 Hatalar

- Bir ilişki fikri hata verirse **yalnızca o fikir düşer**; diğer fikirler ve
  haber devam eder, neden kaydedilir.
- Bir haberin işlenmesi yarıda kalırsa **otomatik yeniden deneme yok**; haber
  admin panelindeki **başarısız haberler** listesine düşer, oradan elle yeniden
  başlatılır.
- Haber başına **süre sınırı yok** (veri beklenebilir). Ajanlar da veri çekme
  işleri gibi "yaşıyorum" sinyali verir; ne veri bekleyen ne de sinyal veren
  haber takılmış sayılır ve başarısız haberler listesine düşer.

## 5. Veri

- Kaynaklar: **TÜİK ve TCMB** ile başlanır; yeni kaynakları kullanıcı zamanla
  ekler.
- Veri **sıfırdan** çekilir.
- **Çekirdek seriler** önceden, **2000-01-01'den itibaren** yüklenir. Hangi
  serilerin çekirdek olacağına Faz 1'de karar verilir.
- Diğer seriler **talep üzerine**, yine **2000-01-01'den itibaren** çekilir.
- Talep üzerine çekilen seri **otomatik olarak çekirdeğe alınmaz.**
- Her değerin **ne zaman çekildiği** en baştan doğru kaydedilir (eski projede bu
  bilgi sonradan eklendi ve eski verinin %99,9'u "zamanı bilinmiyor" kaldı).
- **Revizyonlar saklanır:** kurum eski bir dönemin değerini düzeltirse düzeltme
  yeni kayıt olur, eski değer silinmez. İlişki testi ve grafikler son değeri
  kullanır; eski değerler ileride "haber anında bilinen değer" için gerekir.
- **TÜİK/TCMB'de olmayan göstergeler** (ör. asgari ücret) **elle güncellenen
  bir tabloda** tutulur. Hangi göstergelerin gireceği ve nasıl girileceği Faz
  1'de kararlaştırılır.
- **Veri kesilme uyarısı:** bir kaynaktan veri sessizce gelmemeye başlarsa ya
  da cevabın biçimi değişirse **admin panelinde uyarı** görünür (bildirim yok).
  Hangi durumda uyarı verileceği Faz 1'de kararlaştırılır.

## 6. Haberler

- Kaynaklar: mevcut 5 RSS — Sabah ekonomi, Habertürk ekonomi, Sözcü ekonomi,
  BloombergHT, CNN Türk finans.
- RSS özeti değil, haber sayfasındaki **tam metin** çekilir.
- Haberler **15 dakikada bir** çekilir.
- Her haber işlenir; tekrar haber kontrolü MVP'de yok.

## 7. Katalog ve arama

- Seriler **kapalı bir etiket listesinden** etiketlenir (kavram ağacı). Okuyucu
  için tek cümlelik açıklama tutulur ama aramada kullanılmaz.
- Kavram ağacı (`docs/catalog/concept-tree-draft.md`, eski 18 ana dal) Faz
  2'de kullanıcıyla yeniden gözden geçirilip kesinleştirilir.
- **Etiketleme:** tek etiketleyici **Jev**. Güven eşiğinin üstü otomatik kabul;
  altındakileri Claude tek tek inceler. Eşik **kalibrasyonla** belirlenir: ilk
  ~200 seri Jev ile etiketlenip Claude tarafından kontrol edilir, hataların
  yığıldığı güven seviyesine göre eşik seçilir ve buraya yazılır. Önce TypeSafe kredisi, bitince
  OpenRouter'daki Jev. Etiketleme yalnızca meta bilgiyle yapılır (ad, kurum
  kategorisi/tablosu, birim, frekans, kırılım); değer okunmaz.
- **Arama (embedding yok):**
  1. Ajan isteğini düz dille yazar (ör. "Türkiye geneli toplam konut satış
     sayısı, aylık"). Ajan etiketleri görmez.
  2. Jev etiket ağacındaki **her etiketi** istekle karşılaştırıp **1-5 puanlar**;
     4-5 alanlar geçer.
  3. Kod, geçen etiketlerden **herhangi birini** taşıyan serileri arar.
  4. Jev gelen serileri yeniden **1-5 puanlar** (liste büyükse parçalar halinde).
  5. **4-5 alan seriler** ajana gider (ad + meta bilgi, değer yok). Hiçbiri 4-5
     almazsa "güçlü eşleşme yok" döner; ajan isteğini değiştirip yeniden
     arayabilir (en fazla 3-4 arama).
- Embedding ile sıralama yalnızca ölçüm yetersiz çıkarsa eklenir; o durumda
  model OpenRouter'daki `openai/text-embedding-3-large` olur.
- Arama kalitesi **30-40 sorguluk** bir test setiyle ölçülür, sonra büyütülür.

## 8. İlişki testi

- **Sade test** (kod yapar): korelasyon + gecikme araması; **3 ve daha fazla
  üyeli** ilişkilerde regresyon.
- İlişki testleri **haber tarihinden bağımsızdır**: eldeki **tüm veri**
  (haberden sonraki veri dahil) kullanılır.
- İki test dönemi: **bütün yıllar** ve **2017–2026** (pandemi öncesi ve
  sonrası).
- En az gözlem: **aylık 36, üç aylık 12, yıllık 10.** 3+ serili ilişkilerde
  (regresyon) en az gözlem **frekans eşiği × açıklayıcı seri sayısı** (ör. 2
  açıklayıcılı aylık ilişki en az 72 ay); her açıklayıcının gecikmesi kendi
  ikili gecikme aramasından gelir.
- Her dönemin sonucu: **destekleniyor / desteklenmiyor / veri yetersiz.** İki
  dönemin sonucu da ayrı ayrı saklanır. İlişkinin durumu: ikisi de destekliyorsa
  **destekleniyor**, ikisi de desteklemiyorsa **desteklenmiyor**, farklıysa
  **zamanla değişmiş**; veri yetersizlikte ilgili dönem "veri yetersiz" kalır.
- **Dönüşüm:** test ham seviyeler üzerinde **yapılmaz** (zamanla artan iki seri
  sahte ilişki verir). Graph ajanı hipotezde, veriyi görmeden, kapalı listeden
  seçer ve kilitler: **yıllık % değişim**, **dönemsel % değişim** (aylık /
  çeyreklik) veya **fark** (faiz, işsizlik gibi oranlar için).
- **Frekans eşleştirme:** seriler düşük frekansa indirilir. Fiyat / oran / endeks
  → dönem **ortalaması**; akım (tutar, miktar: ihracat, satış adedi) → dönem
  **toplamı**. Serinin türü katalogdaki Jev etiketinden gelir.
- **Enflasyondan arındırma:** graph ajanı hipotezde nominal **TL tutarı** olan
  serileri işaretler; kod bunları **TÜFE** ile reel hale getirir. Fiyat
  endeksleri, oranlar, adetler ve döviz cinsinden tutarlar arındırılmaz.
- **Yeniden test:** aynı ilişki yeni bir haberle tekrar gelirse ve son sonuç
  "veri yetersiz" ya da son test **30 günden eskiyse**, ilişki güncel veriyle
  kodla yeniden test edilir (hipotez aynı kalır, yeni test ayrı kayıt olur).
  Periyodik toplu yeniden test yoktur.
- Test sonucu **okuyucuya gösterilmez**; admin onay ekranında görünür.
- MVP'de olmayanlar: aile düzeyinde çoklu test düzeltmesi, periyodik otomatik
  yeniden test, ortak sürücü kontrolü, LLM hakemi.

## 9. Graf

- Graf **Neo4j**'dedir ve başta boştur; her haberle büyür.
- **İş bölümü:** PostgreSQL ana veritabanıdır (seriler, değerler, haberler,
  grafikler, onaylar, çekme işleri). İlişkiler ve test kayıtları için **asıl
  kaynak Neo4j**'dir; ilişki bilgisi iki yerde tutulmaz.
- **İlişki kendi düğümüdür**: 2+ seriye bağlıdır; mekanizma, beklenen yön,
  gecikme aralığı ve durum taşır.
- **Her test ayrı kayıttır**, üzerine yazılmaz.
- İlişki **kaynak haberlerine bağlanır** (bir ilişkiye çok haber).
- **Aynı ilişki:** aynı seri kümesi + aynı roller (kim etkiliyor, kim
  etkileniyor). Gecikme aralığı, dönüşüm ve mekanizma metni farklı olsa da aynı
  ilişkidir; yeni haber kaynak olarak eklenir. Ters yön ayrı bir ilişkidir.
- **Reddedilen ilişkiler silinmez**; birinci ajan aynı fikri tekrar önermesin
  diye saklanır.
- Admin panelinde graf görünümü yok; graf **Neo4j Browser**'dan izlenir.

## 10. Görselleştirme

- Grafik kütüphanesi **Vega-Lite**: aynı tarif hem sitedeki interaktif grafiği
  hem sosyal medya PNG'sini üretir.
- Grafik türleri: çizgi, endeksli çizgi (çift eksen yok), gecikmeli dağılım,
  çubuk.
- Her grafik için tarif, kullanılan veri sürümleri ve kaynak notu saklanır.
- **Dönem:** görselleştirme ajanı habere göre kapalı listeden seçer: son 2 yıl,
  son 5 yıl, 2017'den beri, 2000'den beri. Okuyucu sitede dönemi değiştirebilir.
- **Seviye / değişim:** ajan seçer. Haber görselinde habere uyan (fiyat haberi →
  seviye, enflasyon → yıllık %); ilişki grafiğinde testte kullanılan dönüşüm.
- Haber başına en fazla **5 haber görseli + 5 ilişki grafiği**; fazlası
  silinmez, saklanır.
- **Kod kontrolü** başarısız olursa grafik görselleştirme ajanına geri
  bildirimle döner, **en fazla 2 tur**; hâlâ başarısızsa yayımlanmaz, admin
  incelemesi için saklanır.
- Grafik **yayın anında** en güncel veriyle çizilir; kaynak notunda verinin son
  dönemi yazar (ör. "Veri: TÜİK, Eylül 2026'ya kadar").
- Haber yeni bir dönemden söz ediyor ama veri henüz yoksa birinci ajan veri
  çekme ajanına sorar; görsel veri gelene kadar park edilir.

## 11. Site ve yayın

- **Herkese açık web sitesi.** Haber sayfasında: haber başlığı ve kaynağa link,
  interaktif grafikler, veri tablosu ve kaynak notu.
- Başta **her yayın admin onayından** geçer. Onay yükü (günde yüzlerce haber
  olabilir) arttığında yayın otomatikleştirilir.
- **Bildirim yok:** onay bekleyen haber sayısı admin panelinde görünür.
- **İlişki grafiklerinden hangilerinin yayımlanacağına admin karar verir**;
  test sonucu yalnızca admin ekranında görünür.
- Sonuç boşsa (hiç grafik yoksa) haber yayımlanmaz.
- Sosyal medya için **PNG** üretilir; paylaşımı admin **elle** yapar.
- **Admin girişi:** tek admin, kullanıcı adı + şifre (şifre `.env`'de hash
  olarak), oturum çerezi.
- **Geri çekme:** admin yayımlanmış bir haberi siteden kaldırabilir.
- **İzleme ekranları (admin paneli):** veri kaynaklarının durumu (çalışıyor mu,
  son çekme zamanı, hata var mı) ve haberlerin hangi aşamada olduğu.
- Ana sayfa ve site tasarımı Faz 5'te kararlaştırılır.

## 12. Teknoloji

- Yığın eski projeyle aynı araçlardan oluşur: **PostgreSQL, Neo4j, Celery +
  Redis, MinIO, Docker Compose**; backend **FastAPI + Python 3.12**, paket
  yönetimi **uv**; frontend **Next.js + Tailwind**; grafikler **Vega-Lite**.
- TÜİK/TCMB'den gelen **ham cevaplar** MinIO'da saklanır.
- Kurulum tamamen yenidir: yeni GitHub reposu **`karven`**, yeni compose.
- **Adlandırma:** veritabanı tablo/sütun adları, dosya adları ve koddaki adlar
  **İngilizce**dir.
- **LLM çağrılarının ham kaydı** (istek + cevap) MinIO'da saklanır; anahtarlar
  maskelenir.
- **CI yok** (MVP'de testler yerelde çalıştırılır).
- MVP **bu bilgisayarda** çalışır, site dışarıya açılmaz; sunucu ve alan adı MVP
  sonrasında seçilir.
- Belgelerin dosya adları İngilizce, içerikleri **Türkçe**dir.

## 12b. Çalışma düzeni

- **Git:** işler yerel bir `work` dalında istenildiği kadar commit ile yapılır.
  Push **yalnızca kullanıcı söyleyince** yapılır: `work` dalı `main`'e squash ile
  tek commit olarak alınır ve gönderilir. Push edilmiş geçmiş yeniden yazılmaz
  (force push yok).
- **Worktree:** normalde tek `work` dalı ve tek klasörle çalışılır. Worktree
  yalnızca aynı anda birden fazla iş (ör. iki ajan paralel kod yazıyorsa)
  yürürken kullanılır ve **açılmadan önce kullanıcı onayı alınır** (Claude dahil
  hiçbir ajan onaysız worktree açmaz). İş bitince dal `work`'e birleştirilir;
  worktree klasörü ve dalı hemen silinir.
- **Testler:** kod değişikliğinden sonra ilgili unit testler; task sonunda tam
  unit paketi; entegrasyon testleri yalnız veritabanı / Docker / dış servis kodu
  değişince, faz sonunda ve push öncesinde; canlı test (gerçek haber ve LLM)
  yalnızca kullanıcı isteyince.
- **Yedek:** yalnızca riskli işlerden (migration vb.) önce, kullanıcı söyleyince
  ya da önerilip kullanıcı onaylayınca alınır; otomatik ya da her seferinde
  yedek alınmaz.
- Belgelerde olmayan ayrıntılar kullanıcıya sorulur. Kodu kimin yazacağına
  kullanıcı karar verir.

## 13. Eski projeden alınan dersler (tasarım uyarıları)

Bunlar karar değil, eski projede yaşanmış ve tekrarlanmaması gereken
sorunlardır; ilgili fazlarda kabul kriteri olarak kullanılır.

- LLM araç turlarında JSON şeması **son tura kadar gönderilmemeli**; aynı turda
  şema istenince modeller araç çağırmayı bıraktı (EVREN ve OpenRouter).
- Sağlayıcılar geçerli JSON'un sonuna `<|im_end|>` gibi şablon belirteçleri
  sızdırabiliyor; ayrıştırıcı bunları temizlemeli, başka fazlalığa izin
  vermemeli.
- Sağlayıcı hız sınırı (429) için kısa değil, uzun ve artan bekleme gerekiyor.
- Her değerin **çekilme zamanı** en baştan kaydedilmeli.
- Test ortamı canlı ortamla **port, Docker imaj etiketi ve veritabanı
  paylaşmamalı**.
- Canlıda uçtan uca çalışan küçük bir akış, testte çalışan büyük bir sistemden
  önce gelir: yeni özellik eklemeden önce çekirdek akış canlıda kanıtlanmalı.

## Proje içinde verilecek kararlar

Bunlar açık soru değildir; bilinçli olarak ilgili faz sırasında kullanıcıyla
kararlaştırılacaktır. Karar verilince yukarıdaki ilgili bölüme yazılır.

1. **Çekirdek seri listesi** — Faz 1, Task 1.4.
2. **Kavram ağacı** — Faz 2, Task 2.1.
3. **Jev etiketleme eşiği** (kalibrasyon sonucu) — Faz 2, Task 2.3.
4. **Arama test setinin başarı eşiği** — Faz 2, Task 2.6.
5. **Site tasarımı ve ana sayfa** — Faz 5, Task 5.1.
6. **Elle girilen göstergeler** (hangileri, nasıl girilecek) — Faz 1, Task 1.8.
7. **Veri kesilme uyarısının kuralı** (hangi durumda uyarı) — Faz 1, Task 1.4.
