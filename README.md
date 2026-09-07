# 데스게임 보고서 한글 패치

**대상 게임: The Death Game Report Windows판 1.0.4.** 설치 시 원본 파일의 해시를 확인하며, 다른 빌드에는 적용되지 않습니다.

- `data/`: 한국어 번역 및 빌드용 데이터
- `tools/`, `scripts/`, `tests/`: 패치 소스와 검증 도구
- `assets/`: 한글 폰트와 라이선스
- [Releases](https://github.com/yoodong724/drepo-ko/releases): 설치용 패치 ZIP

원본 게임 파일은 포함하지 않습니다. 소스 빌드는 [BUILD.md](BUILD.md)를 참고하세요.

## 설치

1. 게임을 종료하고 Releases에서 `death-game-report-ko-v0.1.0-rc10.zip`을 받습니다.
2. `drepo.exe`와 `drepo.pck`가 있는 게임 폴더에 압축을 풉니다.
3. 생성된 `death-game-report-ko-v0.1.0-rc10` 폴더 안의 `install.cmd`를 실행합니다.

제거는 같은 폴더의 `uninstall.cmd`를 실행합니다. 별도 프로그램 설치는 필요 없습니다.
