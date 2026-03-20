## tmux란

터미널 멀티플렉서. 하나의 SSH 접속 안에서 여러 터미널을 만들 수 있고, 접속을 끊어도 프로세스가 유지됨.

### 핵심 구조

```
세션 (Session)
  └─ 윈도우 (Window)    ← 탭처럼 전환
       └─ Pane          ← 화면 분할
```

- **세션**: 독립된 작업 공간. SSH 끊어도 세션 안의 프로세스는 계속 살아있음.
- **윈도우**: 세션 안의 탭. 이름 붙여서 구분 (예: `train`, `debug`, `gcd`).
- **Pane**: 윈도우 안에서 화면 분할한 각 영역. 각 pane에 독립된 셸이 돌아감.

### 왜 쓰는가

1. **SSH 끊김에 안전.** 학습이나 합성 같은 장시간 작업이 SSH 끊김으로 죽지 않음.
2. **여러 작업 동시 관리 가능.** 디자인별로 윈도우 만들어서 병렬 작업하기 좋음.
3. **에이전트와 터미널 공유 가능.** 에이전트가 tmux pane에 명령 보내고 결과 읽으면, 사람도 같은 pane에서 상태 확인 가능.

### 기본 조작

**Window**

| 동작 | 키 |
| --- | --- |
| 새 윈도우 생성 | `Ctrl+b c` |
| 해당 번호 윈도우로 이동 | `Ctrl+b 숫자(0~9)` |
| 현재 윈도우 닫기 | `Ctrl+b &` (확인 y) |

**Pane**

| 동작 | 키 |
| --- | --- |
| 세로 분할 (좌/우) | `Ctrl+b %` |
| 가로 분할 (상/하) | `Ctrl+b "` |
| 방향 pane으로 이동 | `Ctrl+b ←/→/↑/↓` |
| pane 순서 회전 | `Ctrl+b Ctrl+o` |
| pane 번호 확인 | `Ctrl+b q` |
| 현재 pane 닫기 | `Ctrl+b x` (확인 y) |

**Session**

| 동작 | 키 |
| --- | --- |
| 세션에서 빠져나오기 (유지) | `Ctrl+b d` |

---

## tmux_alias — tmux 단축 명령어

tmux 기본 명령어(`tmux new-session`, `tmux attach`, ...)는 길고 번거로움.
짧은 명령어로 대체해주는 도구.

> seda-leo: `/home/users/kiyoshi/tools/tmux_alias`
> 

### 설치

```bash
cd /path/to/tmux_alias
./install.sh
# `~/.local/bin`에 심볼릭 링크 생성.
```

### 명령어

| 명령어 | 설명 | 예시 |
| --- | --- | --- |
| `lst` | 세션/윈도우/pane 목록 (계층적 출력) | `lst`, `lst -v` |
| `pwt` | 현재 위치 출력 | `pwt` → `t1:0.0` |
| `cdt` | 세션/윈도우 접근 (없으면 생성) | `cdt t1:train` |
| `mkt` | 세션/윈도우/pane 생성 | `mkt exp:{gcd,aes,ibex}` |
| `mvt` | 세션 rename / 윈도우·pane 이동 | `mvt t1:0 t5:train` |
| `rmt` | 세션/윈도우/pane 삭제 | `rmt t1:train` |

### 활용 예시

```bash
# 세션 만들고 디자인별 윈도우 한 번에 생성
mkt exp:{gcd,aes,ibex,jpeg}

# 윈도우 이름 변경
mvt exp:0 exp:baseline

# 현재 위치 확인
pwt  # → exp:gcd.0

# 전체 세션 확인
lst
```

---

## mcp-tmux-injector — CLI 에이전트 tmux 연결

Claude Code, Codex 같은 CLI 에이전트가 tmux pane에 명령 주입하고 결과 수집하는 MCP 서버.
Python REPL, TCL (OpenROAD, Innovus, Genus 등), Shell 모두 호환

> seda-leo: `/home/users/kiyoshi/tools/mcp-tmux-injector`
> 

### 설치

```bash
cd /path/to/mcp-tmux-injector
pip install -e .
```

### MCP 클라이언트 설정

**Claude Code (유저 전역)**

```bash
claude mcp add tmux-injector --scope user -- uv run --directory /path/to/mcp-tmux-injector mcp-tmux-injector
```

**Codex CLI**

```bash
codex mcp add tmux-injector -- $(which mcp-tmux-injector)
```

### 에이전트가 할 수 있는 것

내부적으로 세 가지 실행 모드 × 세 가지 언어 = 9개 tool로 동작.

| 모드 | 동작 | 용도 |
| --- | --- | --- |
| blocking | 명령 완료까지 대기 | 짧은 명령, 결과 바로 사용 |
| background | 즉시 리턴, 나중에 결과 확인 | 장시간 작업 |
| peek | 일정 시간 대기 후 화면 캡처 | 인터프리터 시작/종료 |

|  | blocking | background | peek |
| --- | --- | --- | --- |
| **Python** | `xpy` | `xpy_start` | `xpy_peek` |
| **TCL** | `xtcl` | `xtcl_start` | `xtcl_peek` |
| **Shell** | `xsh` | `xsh_start` | `xsh_peek` |

그 외에 pane 화면 캡처, 태스크 모니터링, 세션/pane 관리 tool이 있고 에이전트가 상황에 맞게 알아서 씀.

### 사용 예시

에이전트에게 자연어로 지시하면 됨. 아래는 실제로 시킬 수 있는 것들.

**세션 만들고 디자인별 병렬 작업 (EDA)**

> "timing_opt 세션에 gcd, aes, ibex, jpeg 윈도우 만들고 각각 openroad 켜서 run_*.tcl 스크립트 돌려"
> 

세션 이름을 태스크에 맞게, 윈도우 이름을 디자인 이름으로 지정하면 관리 편함.
사람이 `mkt`로 직접 만든 세션을 에이전트에게 쓰게 할 수도 있고, 에이전트한테 세션 생성부터 시킬 수도 있음.

**장시간 학습 모니터링**

> "[train.py](http://train.py/) 백그라운드로 돌려. 끝나면 결과 알려줘"
> 

에이전트가 background 모드로 실행하고 완료될 때까지 알아서 대기.

**각 윈도우 상태 확인**

> "지금 timing_opt 세션 각 윈도우 상태 확인해줘"
> 

에이전트가 세션 상태를 조회해서 프로세스 상태, cwd, GPU 메모리 등을 보여줌.

**SSH 접속해서 원격 작업**

> "이 pane에서 ssh로 다른 서버 접속하고 거기서 작업해"
> 

에이전트가 SSH 접속, 비밀번호 입력까지 처리 가능.
