# 소스 빌드

Linux/WSL, Python 3.10 이상, Windows판 게임 1.0.5 원본이 필요합니다. 게임 파일은 `drepo/1.0.5/`에 둡니다. 아래 명령은 저장소 루트에서 실행합니다.

```bash
bash scripts/bootstrap_gdre.sh
bash scripts/bootstrap_godot.sh
mkdir -p build/localization/canonical
.tools/gdre-v2.6.4/gdre_tools.x86_64 --headless --recover=drepo/1.0.5/drepo.pck --output=build/localization/recovered
python3 tools/prepare_public_source.py --translations data/localization/translations.tsv --recovered-dir build/localization/recovered --output build/localization/canonical/segments.tsv
python3 tools/integrate.py \
  --build-id DGR-WIN-1.0.5-b3e7048e \
  --source-exe drepo/1.0.5/drepo.exe --source-pck drepo/1.0.5/drepo.pck \
  --recovered-dir build/localization/recovered \
  --segments build/localization/canonical/segments.tsv \
  --source-manifest data/localization/source_manifest.tsv \
  --assignments data/localization/assignments.tsv \
  --expected-segments-sha256 "$(sha256sum build/localization/canonical/segments.tsv | cut -d' ' -f1)" \
  --expected-manifest-sha256 "$(sha256sum data/localization/source_manifest.tsv | cut -d' ' -f1)" \
  --expected-assignments-sha256 "$(sha256sum data/localization/assignments.tsv | cut -d' ' -f1)" \
  --assets-root assets/fonts \
  --godot .tools/godot-v4.6.3/Godot_v4.6.3-stable_linux.x86_64 \
  --gdre .tools/gdre-v2.6.4/gdre_tools.x86_64 \
  --japanese-allowlist data/localization/japanese_allowlist.json \
  --preserved-fragments data/localization/preserved_japanese.json \
  --output-root build/localization/integrated
mkdir -p dist
python3 tools/patch_release.py create \
  --source-exe drepo/1.0.5/drepo.exe --source-pck drepo/1.0.5/drepo.pck \
  --localized-pck build/localization/integrated/drepo.ko.pck \
  --integration-manifest build/localization/integrated/integration-manifest.json \
  --output-dir dist/death-game-report-ko-v1.0.5
python3 tools/patch_release.py audit \
  --package-dir dist/death-game-report-ko-v1.0.5 \
  --source-exe drepo/1.0.5/drepo.exe --source-pck drepo/1.0.5/drepo.pck
python3 -m unittest tests.test_patch_release -q
```

`translations.tsv`에는 원문과 앞뒤 문맥을 넣지 않습니다. 준비 도구가 로컬 게임에서 추출한 문자열의 해시를 확인하고 빌드용 TSV를 생성합니다. 번역 수정은 `translations.tsv`, 코드·레이아웃 수정은 `tools/`에 반영합니다. 출력 경로는 새 경로여야 합니다.

배포 대상은 `dist/`의 차분 패치 폴더입니다. `drepo/`, `build/`, `.tools/`와 원문이 채워진 TSV는 공개하지 않습니다. 설치기 파일을 변경하면 `tools/patch_release.py`의 `INSTALLER_SHA256`도 갱신해야 합니다.
