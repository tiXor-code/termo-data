# termo-data

Arhiva istorica a intreruperilor de apa calda si caldura din Bucuresti, construita prin
snapshot-uri la 15 minute ale paginilor publice CMTEB / Termoenergetica:

- [Functionare sistem termoficare](https://www.cmteb.ro/functionare_sistem_termoficare.php) -
  lista oficiala a intreruperilor curente (sector, punct termic, strazi si blocuri afectate,
  cauza, estimare remediere). Salvata ca `data/functionare.html`.
- [Harta stare sistem termoficare](https://www.cmteb.ro/harta_stare_sistem_termoficare_bucuresti.php) -
  starea fiecarui punct termic (~950), cu coordonate. Salvata ca `data/harta.html`.

Inregistrarile rezolvate dispar de pe site si nu exista nicio arhiva oficiala, deci istoricul
se poate construi doar prin colectare continua. Acest repo face exact asta: un GitHub Action
ruleaza la 15 minute si comite paginile doar cand continutul se schimba (hash pe corpul
raspunsului - headerele Last-Modified ale site-ului sunt nesigure).

## De ce

Datele alimenteaza un proiect civic: cate zile pe an sta fiecare zona din Bucuresti fara apa
calda. Site-ul si pipeline-ul de procesare vor fi publicate separat.

## Multumiri

Modelul de git-scraping este inspirat de
[FlorinPopaCodes/termoficare-data](https://github.com/FlorinPopaCodes/termoficare-data)
(arhiva din decembrie 2021 incoace) si [gov2-ro/prometeu](https://github.com/gov2-ro/prometeu).

Limitele sectoarelor (`static/sectoare.geojson`) sunt derivate din OpenStreetMap - (c) [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), licenta [ODbL](https://opendatacommons.org/licenses/odbl/).

## Licenta

Codul: MIT. Datele din `data/` reproduc informatii publice publicate de
CMTEB / Termoenergetica SA pe cmteb.ro.
