# Faz 5 — Site ve admin onayı

## Faz amacı

Sonuçları herkese açık bir web sitesinde yayımlamak ve yayından önce admin
onayını kurmak. Bu faz MVP'nin ana milestone'udur: gerçek bir haber sisteme
girip uçtan uca işlenir, admin onaylar ve sitede görünür.

## Task 5.1 — Herkese açık haber sayfası

**Repo:** `karven`
**Alan:** `frontend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 4.4

**Referanslar:** `docs/DECISIONS.md` §11

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Haber başlığı ve kaynağa link (haber metninin kendisi yayımlanmaz).
- [ ] İnteraktif grafikler (Vega-Lite).
- [ ] Veri tablosu ve kaynak notu.
- [ ] İlişki testi sonucu okuyucuya **gösterilmez**.
- [ ] Site tasarımı ve ana sayfa kullanıcıyla belirlenir (**proje içinde karar**,
      `docs/DECISIONS.md` → Proje içinde verilecek kararlar #5).

**Kabul kriteri:** Onaylanmış bir haberin sayfası başlık/link, interaktif
grafikler, veri tablosu ve kaynak notuyla açılıyor.

## Task 5.2 — Admin onay ekranı

**Repo:** `karven`
**Alan:** `frontend`, `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 4.4

**Referanslar:** `docs/DECISIONS.md` §10, §11

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Onay bekleyen haberler: grafikler ve ilişki testi sonuçları admin'e görünür.
- [ ] Bildirim yok; onay bekleyen haber sayısı panelde görünür.
- [ ] Admin hangi **ilişki grafiklerinin** yayımlanacağını seçer.
- [ ] Onay / red; onay anında grafikler güncel veriyle çizilir (Task 4.4).
- [ ] Hiç grafik yoksa haber yayımlanmaz.
- [ ] Kontrolden geçemeyip saklanan grafikler de burada görülebilir.
- [ ] **Geri çekme:** yayımlanmış bir haber siteden kaldırılabilir.
- [ ] Sosyal medya PNG'leri buradan indirilir; paylaşım elle yapılır.
- [ ] Admin girişi: tek admin, kullanıcı adı + şifre (şifre `.env`'de hash),
      oturum çerezi.

**Kabul kriteri:** Admin bir haberi ilişki grafiklerinden bir kısmını seçerek
onaylıyor ve haber sitede yalnız seçilen grafiklerle görünüyor.

## Task 5.3 — Çekilemeyen veriler ve başarısız haberler listeleri

**Repo:** `karven`
**Alan:** `frontend`, `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 1.6, Task 3.5

**Referanslar:** `docs/DECISIONS.md` §4.4

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Admin panelinde liste: hangi veri, neden çekilemedi, veri çekme ajanının
      önerisi, hangi haber/fikir için istendi.
- [ ] Başarısız haberler listesi: hangi adımda, neden; elle yeniden başlatma.

**Kabul kriteri:** Çekilemeyen gerçek bir talep listede nedeni ve öneriyle görünüyor.

## Task 5.4 — MVP başarı senaryosu (uçtan uca, canlıda)

**Repo:** `karven`
**Alan:** `root`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 5.1, Task 5.2, Task 5.3, Task 5.5

**Referanslar:** `docs/DECISIONS.md` §2, §3, §13

**Hedef dosyalar:** —

### Checklist

- [ ] Gerçek bir haber RSS'ten gelir, tam metni çekilir.
- [ ] Birinci ajan haber görsellerini ve ilişki fikirlerini çıkarır.
- [ ] En az bir ilişki graph ajanında test edilip grafa yazılır.
- [ ] Grafikler üretilir, kodla kontrol edilir.
- [ ] Admin onaylar; haber sayfası sitede yayımlanır; PNG indirilebilir.
- [ ] Süre ve LLM maliyeti kaydedilir.

**Kabul kriteri:** Yukarıdaki akış canlı ortamda en az birkaç farklı haberde
elle müdahale olmadan (admin onayı hariç) çalışıyor.

## Task 5.5 — İzleme ekranları, uyarılar ve prompt yönetimi

**Repo:** `karven`
**Alan:** `frontend`, `backend`
**Durum:** Başlamadı
**Bağımlılıklar:** Task 0.6, Task 1.4, Task 3.5

**Referanslar:** `docs/DECISIONS.md` §4, §5, §11

**Hedef dosyalar:** Faz 0 yapısına göre belirlenir.

### Checklist

- [ ] Veri kaynaklarının durumu: çalışıyor mu, son çekme zamanı, hata var mı.
- [ ] Haberlerin hangi aşamada olduğu.
- [ ] Veri kesilme uyarıları (Task 1.4) panelde görünür; bildirim yok.
- [ ] Prompt yönetimi: ajan başına sürümleri görme, yeni sürüm ekleme,
      etkinleştirme (Task 0.6).
- [ ] Container sağlığı: servis başına bellek sınırına takılma (OOM) ve
      yeniden başlama sayısı, son 24 saat (kullanıcı kararı 2026-09-30;
      Docker yeniden başlattığı için sessiz kalmamalı).

**Kabul kriteri:** Admin panelinde kaynak durumları ve haber aşamaları gerçek
veriyle görünüyor; yapay olarak tetiklenen bir veri kesilme uyarısı panelde
çıkıyor; panelden eklenen prompt sürümü etkinleşiyor.
