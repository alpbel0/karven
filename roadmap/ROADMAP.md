# Karven — Roadmap (yeni proje, MVP)

> Bu doküman "ne inşa edeceğiz" değil (bkz. `docs/DECISIONS.md`), "hangi
> sırayla inşa edeceğiz" dokümanıdır. Tarih/süre tahmini yok — sadece faz
> sırası, her fazın amacı ve her fazın kendi dosyasındaki task kırılımı.
> Her faz `roadmap/` klasöründe kendi dosyasındadır; task'lar orada
> Repo/Alan/Durum/Bağımlılıklar/Referanslar/Hedef dosyalar/Checklist/Kabul
> kriteri alanlarıyla ayrıntılandırılmıştır.
>
> Proje 2026-09-29'da sıfırdan yeniden başladı: kod, veri, GitHub reposu ve
> Docker kurulumu yenidir. Eski projenin roadmap'i bu projeye alınmamıştır.

## MVP hedefi

Bir ekonomi haberi sisteme girer; birinci ajan haberde anlatılanları ve
haberden çıkan ekonomik ilişkileri bulur; ilişkiler knowledge graph ajanında
resmî TÜİK/TCMB verisiyle test edilip grafa yazılır; grafikler çizilir, kodla
kontrol edilir ve admin onayından sonra herkese açık sitede yayımlanır.
(Ayrıntılar: `docs/DECISIONS.md`.)

## Fazlar

- [Faz 0 — İskelet](phase-0-skeleton.md) — Yeni GitHub reposu, monorepo, Docker
  Compose, migrator, canlıdan ayrı test ortamı ve LLM istemcisi (EVREN +
  OpenRouter yedek) ve prompt'ların veritabanında sürümlü saklanması.
- [Faz 1 — Veri katmanı](phase-1-data-layer.md) — Ortak veri modeli, TÜİK ve
  TCMB bağlayıcıları, çekirdek seriler (2000+), talep üzerine çekme, veri
  çekme ajanı, RSS haber kaynakları (tam metin) ve elle girilen göstergeler.
- [Faz 2 — Katalog](phase-2-catalog.md) — Kavram ağacı, Jev ile etiketleme,
  Jev + kod + Jev katalog araması, veri durumu aracı ve arama test seti.
- [Faz 3 — İlişki akışı](phase-3-relation-flow.md) — Birinci ajan, knowledge graph
  ajanı, sade test motoru ve Neo4j ilişki grafı.
- [Faz 4 — Görselleştirme](phase-4-visualization.md) — Vega-Lite tarif formatı,
  görselleştirme ajanı, kodla kontrol, yayın anında çizim ve PNG.
- [Faz 5 — Site ve admin onayı](phase-5-site-admin.md) — Herkese açık haber
  sayfası, admin onay ekranı (geri çekme dahil), çekilemeyen veriler listesi,
  izleme ekranları ve uyarılar, prompt yönetimi ve MVP başarı senaryosu (ana
  milestone).
- [MVP sonrası](post-mvp.md) — Sadece özellik listesi; ayrıntılar MVP'den
  sonra konuşulacak.

---

## Notlar

- Fazlar arasında sıkı bir "önce bitir sonra geç" kuralı yok; özellikle Faz 1
  (bağlayıcılar) ve Faz 2 (katalog) paralel ilerleyebilir. Gerçek sıra her
  task'ın **Bağımlılıklar** alanındadır.
- Her fazın çıkış kriteri gerçek veriyle doğrulanır; uydurma veriyle "tamam"
  sayılmaz.
- Eski projeden ders: yeni özellik eklemeden önce çekirdek akış **canlıda**
  uçtan uca kanıtlanmalı (`docs/DECISIONS.md` §13).
- Task numaraları sıralama değil **kalıcı kimliktir**: bir task taşınırsa eski
  numarası boş bırakılır, başka bir task'a yeniden atanmaz.
- Açık soru yoktur. Bazı kararlar bilinçli olarak ilgili fazda kullanıcıyla
  verilir (`docs/DECISIONS.md` → **Proje içinde verilecek kararlar**); ilgili
  task'larda "Proje içinde karar" olarak işaretlidir.
- Adlandırma: veritabanı tablo/sütun adları, dosya adları ve koddaki adlar
  İngilizcedir (`docs/DECISIONS.md` §12).
- Toplam task sayısı: **34** (Faz 0: 6, Faz 1: 8, Faz 2: 6, Faz 3: 5, Faz 4: 4,
  Faz 5: 5). `grep -c "^## Task " roadmap/phase-*.md` toplamıyla doğrulanabilir.
