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
- **Kaynak kapsamı (2026-10-01):** her kaynak, sanki her şeyi çekilecekmiş gibi
  **geniş araştırılır** ve belgelenir (`Desktop/KARVEN-ARAŞTIRMA/`); ama
  bağlayıcıya **yalnızca projeye gereken kanallar** alınır. Bir veri sonradan
  gerekirse araştırma notlarından nereden alınacağı bulunur ve o kanal o zaman
  eklenir. TÜİK (Task 1.2) önceki "bütün kanallar" kuralıyla tamamlanır.
- **Önce katalog (2026-10-01):** bağlanan bir kanalın **kataloğu eksiksiz**
  doldurulur (veri setleri + bütün boyut kodları); gözlemler **talep üzerine**
  çekilir. Kodlar yalnız rapor içinde görünüyorsa (ör. TÜİK turizm, seçim) kısa
  bir **keşif** taramasıyla kataloğa eklenir. Toplu önceden yükleme yalnız
  kullanıcı kararıyla yapılır (çekirdek seriler bu kararın parçasıdır).
- **Veri kesilme uyarısı:** bir kaynaktan veri sessizce gelmemeye başlarsa ya
  da cevabın biçimi değişirse **admin panelinde uyarı** görünür (bildirim yok).
  Kural (kullanıcı, 2026-10-03, Task 1.4) üç durumdan biri olunca uyarı kaydı
  açılır: (1) **yeni dönem gelmedi** — yayın takvimindeki tarihten 2 gün
  geçti, değer hâlâ yok; (2) **biçim değişti** — cevapta beklenen alan yok ya
  da HTML throttle sayfası geldi, ilk görüşte; (3) **art arda hata** — üst
  üste 3 çekim hata verdi ya da boş döndü.
- **Çekirdek seriler (2026-10-03, Task 1.4):** en dar liste — TCMB
  `TP.DK.USD.A.EF.YTL` (günlük) ve `TP.CLI2.A01` (aylık), artı TÜİK GSYH 13
  satırı (Turcat → EVDS, çeyreklik). Toplulaştırma yöntemi kaynağın
  `DEFAULT_AGG_METHOD` değeri. Yeni seri ihtiyaç doğdukça eklenir. Güncelleme
  **takvimli yoklama**: EVDS3 `aylikYayinlar` + TÜİK `GetYillikHaberBulteniListesi`
  takvimleri yeni dönemi bekleme zamanını verir (`appg` takvimi alınmaz).
  Zamanlayıcı Task 1.4'te basit tek amaçlı döngü; Celery `worker`/`beat`
  Task 1.5/1.7'de.

## 6. Haberler

- Kaynaklar: mevcut 5 RSS — Sabah ekonomi, Habertürk ekonomi, Sözcü ekonomi,
  BloombergHT, CNN Türk finans.
- RSS özeti değil, haber sayfasındaki **tam metin** çekilir.
- Haberler **15 dakikada bir** çekilir.
- Her haber işlenir; tekrar haber kontrolü MVP'de yok.

## 7. Katalog ve arama

- **Katalog yapısı (2026-09-30):** Katalog **veri seti + boyut listeleri**
  olarak tutulur: her veri seti (TÜİK veri seti, TCMB grubu vb.) için ad,
  kategori, frekans, yıl aralığı, toplam gözlem sayısı ve **her boyutun tam kod
  listesi** (il, sektör, tür, birim …; varsa hiyerarşisiyle). Seri satırları
  önceden yazılmaz; bir kırılım **ilk kez kullanıldığında** seri tablosuna
  eklenir (TÜİK'te bütün kombinasyonlar ~1,74 milyon seri ederdi, çoğu hiç
  kullanılmazdı). Her seri boyut listelerinden bulunabilir; kaynakta verisi
  olmayan bir kombinasyon çekilirken "boş" olarak öğrenilir ve kaydedilir.
  Kaynağın kendi içinde tutarsız olduğu veri setleri (bildirdiği gözlem
  sayısından azını veriyorsa) alınır ama **"kaynak eksik bildiriyor"** notuyla
  işaretlenir.
- **Etiket düzeyi (2026-09-30):** Etiketleme **veri seti düzeyinde** yapılır;
  **bir veri setine birden fazla etiket** verilebilir; seriler veri setinin
  etiketlerini devralır.
- **Arama akışı (2026-09-30; etiket aşaması 2026-10-04'te ağaçla birleştirildi):**
  0. Ajan isteğini düz dille yazar (ör. "Türkiye geneli toplam konut satış sayısı,
     aylık"); ajan etiketleri görmez, her çağrıda en fazla 3-4 arama hakkı vardır.
  1. Veri seti seçimi: Jev önce **ana dalları**, sonra yalnız geçen ana dalların
     **yapraklarını** istekle karşılaştırıp **1-5 puanlar**; 4-5 alanlar geçer.
     Kod, geçen yapraklardan **herhangi birini** taşıyan veri setlerini ve geçen ana
     dalı doğrudan taşıyan çok konulu derlemeleri (Turcat, PKA) getirir;
     `revizyon_tablosu` bayrağı olanlar dışlanır. Jev aday veri setlerini (liste büyükse
     parçalar halinde) yeniden **1-5 puanlar**; 4-5 alanlar sonraki adıma geçer.
  2. Kırılım seçimi: kod boyutları sırayla Jev'e sorar (seçim sorusu),
     **hiyerarşik** (önce düzey sonra yer; önce ana sektör sonra alt sektör).
     Jev'in seçim sorusu en fazla 255 seçenek alır; uzun listeler hiyerarşiyle
     daraltılır.
  3. Güven eşiğin altındaysa aynı soru 2-3 farklı cümleyle sorulur; cevaplar
     uyuşursa kabul, uyuşmazsa en iyi adaylar tutulur.
  4. **Son doğrulama:** seçilen serinin tam adıyla "bu seri isteğe uyuyor mu?"
     sorulur; düşükse sıradaki aday seriye / veri setine geçilir.
  5. Hâlâ belirsizse en iyi 2-3 aday ajana sunulur, seçimi ajan yapar.
  (2026-09-30 denemesi: il seçimi Kayseri 0,91, düzey önce seçilince Bursa 1,0;
  sektör iki adımda Tekstil 0,96, Otomotiv 0,89; doğrulama yanlış veri setini
  0,14 ile yakaladı.) Hiçbir aday 4-5 almazsa "güçlü eşleşme yok" döner; ajan
  isteğini değiştirip yeniden arayabilir (arama sınırı içinde). Ölçüm türü ve
  frekans/birim, seri seçilirken ölçü kodundan okunur (`measure_types`).
- Veri setleri **kapalı bir etiket listesinden** etiketlenir (kavram ağacı).
  Okuyucu için tek cümlelik açıklama tutulur ama aramada kullanılmaz.
- **Kavram ağacı (2026-10-04, v2.1):** `backend/app/catalog/concept_tree.yaml` (makinece
  okunur, her etikette tanım; bütünlüğü `backend/tests/test_catalog_concept_tree.py`
  denetler). Eski 18 dallı taslak (`concept-tree-draft.md`) yerine geçti; 24 ana dal,
  109 yaprak. Kullanıcı önerileri onayladı; Opus 5.5 ve Codex gpt-6-astra iki tur
  inceledi. Kurallar:
  - Atanabilir etiket yapraktır; ana dal gezinme grubudur ve otomatik devralınır.
    Jev önce ana dalları, sonra yalnız geçen ana dalların yapraklarını puanlar.
  - Veri seti 1-3 yaprak alır; aşan kapsam sessizce kesilmez, inceleme kuyruğuna gider.
    İstisna: çok konulu derlemeler (Turcat, TCMB Piyasa Katılımcıları Anketi) yalnız
    ana dal etiketi alır; arama geçen yaprakları ve geçen ana dalı taşıyan bu
    derlemeleri getirir.
  - `degildir` listesi birincil etiketi belirler; komşu etiket ikincil olarak eklenebilir.
  - Etiket alanları: konu, ölçüm türü, veri niteliği. Coğrafya, sektör, sıklık, kurum,
    para birimi, vade, ürün sınıflaması etiket değil, meta bilgi veya kırılımdır
    (`mal_gruplari`, `bolgesel_gsyh` bu yüzden silindi). Uluslararası karşılaştırma dalı yok.
  - **Ölçüm türü ve veri niteliği ölçü kodu/seri düzeyindedir** (anket veri setleri hem
    gerçekleşen hem beklenti taşır); kodlar bir kez türe eşlenir, Jev tür etiketlemez.
    Veri seti, içerdiği değerlerin kümesini taşır.
  - Meta bayrakları: `revizyon_tablosu` (varsayılan aramadan dışlanır),
    `mevsim_arindirilmis`, `para_birimi`, `nominal_mi`, `donem_serisi`, `arsiv`,
    `cok_konulu_derleme`. Task 2.2'de dolduruldu (aşağıda "Katalog zenginleştirme").
  - Etiketleme isteminde tam `category_path` verilir. Kaynak kategori adı tek başına
    yanıltıcıdır (ör. TCMB "MAL GRUPLARI" aslında iktisadi yönelim anketidir); kategori
    ipucu tablosu yalnız denetim içindir (Jev etiketiyle uyuşmazsa inceleme kuyruğu),
    arama istemine eklenmez.
  - Boş yapraklar normaldir; "yüksek puan ama güçlü eşleşme yok" Task 2.6'da izlenir.
    Ölçümde ayrıca "il GSYH" sorgusu bulunmalı.
- **Katalog zenginleştirme (Task 2.2, 2026-10-04; kullanıcı kararları):**
  - Etiketler ayrı `dataset_tags` tablosunda (yaprak/dal, güven, kaynak jev/elle,
    durum kabul/inceleme/red); Task 2.3 doldurur.
  - Okuyucu açıklaması **şablondur** (kurum · kategori · frekans · birim), LLM yok;
    kaynağın kendi açıklaması `attributes.source_description`'da saklanır. LLM ile
    gerçek cümle ileride (BACKLOG). Jev metin yazamaz (yalnız evet/hayır, seçim, puan).
  - "Yüklü mü" saklanmaz, gözlemlerden sorgu anında hesaplanır.
  - Bayraklar `datasets` üzerinde tipli kolonlardır; göstergeye göre değişenler
    (para birimi, nominal/reel, mevsim) gösterge düzeyinde tutulur, veri seti kümesini taşır.
  - `arsiv` yalnız kaynağın kendi "Arşiv" etiketinden gelir; kaynağın listesinden
    kaldırılması (`unlisted_since`) arşiv sayılmaz.
  - **Tür ölçü birleşimine bağlanır** (`measure_combinations`): her veri setinde "ne
    ölçüldüğünü" söyleyen kırılımlar (gösterge, birim, değişim türü) seçilir, kodlarının
    her birleşimine bir tür, veri niteliği, toplama kuralı, para birimi, nominal/reel,
    mevsim ve kümülatif bilgisi verilir. TCMB/HMB'de birleşim = seri kodu; ölçü
    kırılımı olmayan veri setinde tek birleşim.
  - Doldurma: önce kesin kurallar; kalanlarda Jev (kırılım evet/hayır, tür ve nitelik
    seçimi, para birimi seçimi, nominal evet/hayır, dönem serisi evet/hayır). Jev
    güveni **0,60** ve üstü otomatik kabul, altı Claude'un inceleme listesine
    (evet/hayır sorularında ≥0,60 evet, ≤0,40 hayır, arası inceleme). İlk eşik 0,70
    idi (2026-10-04 denemesi: 15 örnekte yanlışların hepsi ≤0,62); canlı geçişte
    kullanıcı 0,60'a indirdi. Kural her zaman Jev'e,
    elle karar her şeye üstündür; kural yeniden çalışınca Jev/elle değerlerine dokunmaz.
  - Kod: `backend/app/catalog/enrich*.py`; CLI `python -m app.catalog.enrich
    rules|jev|report|review-list|set-*`. Katalog yenilemesi açıklamayı ve
    zenginleştirme alanlarını silmez.
- **Etiketleme:** tek etiketleyici **Jev**. Güven eşiğinin üstü otomatik kabul;
  altındakileri Claude tek tek inceler. Eşik **kalibrasyonla** belirlenir: ilk
  ~200 veri seti Jev ile etiketlenip Claude tarafından kontrol edilir, hataların
  yığıldığı güven seviyesine göre eşik seçilir ve buraya yazılır. Önce TypeSafe kredisi, bitince
  OpenRouter'daki Jev. Etiketleme yalnızca meta bilgiyle yapılır (ad, kurum
  kategorisi/tablosu, birim, frekans, kırılım); değer okunmaz.
- **Arama (embedding yok):** yukarıdaki arama akışıdır; sonuç olarak **4-5 alan
  seriler** ajana gider (ad + meta bilgi, değer yok).
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
  **toplamı**; stok (borç stoku, mevduat, nüfus) → **dönem sonu değeri**; yüzde
  değişim serileri **ham seviyeden yeniden hesaplanır** (ortalaması alınmaz, seviye
  yoksa seri eşleştirmeden çıkar); ağırlık, katkı, dağılım istatistiği gibi türler
  ilişki testine girmez. Kümülatif akım serileri toplamadan önce dönemlik değere
  çevrilir. Kural katalogdaki **ölçü kodu → ölçüm türü** eşlemesinden gelir
  (`concept_tree.yaml` `measure_types.toplama`). Kaynağın kendi toplama yöntemi
  (TCMB `DEFAULT_AGG_METHOD`) yalnız `sum`/`avg` ise önceliklidir; `last` (TCMB
  serilerinin ~%84'ünde varsayılan), `max`, `min` güvenilmez sayılır ve bizim tür
  kuralımız geçerli olur. İkisi uyuşmazsa `measure_combinations.aggregation_conflict`
  işaretlenir (2026-10-04, kullanıcı kararı).
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

1. ~~**Çekirdek seri listesi**~~ — karar verildi (§5, 2026-10-03).
2. ~~**Kavram ağacı**~~ — karar verildi (§7, 2026-10-04, v2.1).
3. **Jev etiketleme eşiği** (kalibrasyon sonucu) — Faz 2, Task 2.3.
4. **Arama test setinin başarı eşiği** — Faz 2, Task 2.6.
5. **Site tasarımı ve ana sayfa** — Faz 5, Task 5.1.
6. **Elle girilen göstergeler** (hangileri, nasıl girilecek) — Faz 1, Task 1.8.
7. ~~**Veri kesilme uyarısının kuralı**~~ — karar verildi (§5, 2026-10-03).
