# waydroid-windows

[English](README.md) | **한국어**

GNOME에서 Waydroid의 멀티 윈도우 모드를 쓸 만하게 만듭니다. 안드로이드 앱마다 데스크톱 창이 따로
생기고, 창이 사라지지 않으며, 가장자리로 크기를 바꾸고, 제목 표시줄로 옮기거나 최대화하고, 독으로
최소화할 수 있습니다.

정체는 Waydroid의 하드웨어 컴포저(HWC)와 GNOME 컴포지터 사이에 끼는 Wayland 프록시
`wlproxy.py`입니다. 둘이 어긋나는 몇몇 메시지를 고쳐 쓰고, 데스크톱에서 창에 한 일에 맞춰 안드로이드
태스크를 움직입니다.

## 고치는 것

- **포커스가 바뀌면 창이 사라지는 문제.** Waydroid 벤더 이미지 `lineage-20.0-20260927-MAINLINE`은
  크기가 담긴 `xdg_toplevel.configure`를 받을 때마다 안드로이드 디스플레이를 핫플러그합니다
  ([waydroid#2436](https://github.com/waydroid/waydroid/issues/2436)). GNOME은 포커스가 바뀔 때마다
  그런 메시지를 보내고, 그때마다 앱 창이 전부 지워집니다. 프록시가 그 크기를 0으로 바꿉니다.
- **크기 조절, 이동, 최대화, 최소화.** 창 둘레 12px 테두리로 어느 변이나 모서리에서든 크기를 바꿉니다.
  크기 조절은 GNOME이 하고, 프록시는 안드로이드 태스크에 같은 범위를 주며 안드로이드가 그 크기로
  그릴 때까지 응답을 붙잡아 둡니다. 그래서 왼쪽이나 위쪽 변을 끌어도 반대편 변이 그대로 있습니다.
  제목 표시줄을 끌면 창이 움직입니다. 제목 표시줄의 최대화·최소화 버튼은 데스크톱의 동작으로
  바꿉니다. 안드로이드 안에서 하면 전체 화면 태스크가 다른 태스크를 모두 가리고, 최소화된 태스크는
  창과 독 아이콘을 잃기 때문입니다.
- **창이 줄어들거나 아주 작아지는 문제.** HWC가 분수 배율을 알게 되면 프록시는 그 뒤의
  `wl_output.scale`을 버립니다. GNOME은 모니터 배치가 바뀔 때 이 값을 다시 보내는데, 그러면 HWC가
  세션이 끝날 때까지 모든 창을 0.625배로 그립니다.
- **클릭을 먹는 화면 크기의 키보드.** 하드웨어 키보드가 있어도 안드로이드는 화면 크기의 빈 화상
  키보드 창을 띄웁니다. 안드로이드의 "화상 키보드 사용" 설정이 켜져 있지 않으면 이 창을 띄우지 않습니다.
- **새 창이 멈추는 문제.** HWC 안의 경쟁 상태 때문에 `window::create`가 이벤트를 영원히 기다릴 수
  있습니다. 프록시는 sync 응답마다 빈 이벤트를 보내 깨웁니다.
- **Super 키.** 안드로이드가 이 키로 어시스턴트 검색을 열기 때문에 걸러 냅니다.

태스크는 `wdtask/WdTask.java`로 움직입니다. 작은 dex를 셸 사용자 권한으로, 계속 열어 둔
`adb shell app_process` 하나로 실행합니다. `cmd activity task resize`는 자유형 태스크만 크기를 바꾸고
태스크의 창 모드를 바꾸는 셸 명령은 없어서, 안드로이드의 window organizer를 리플렉션으로 씁니다.

삼성 프레임워크를 돌리는 이미지를 위해 One UI 창 장식도 다룹니다(그림자 여백이 아니라 내용에 맞춘
창 틀, One UI 제목 표시줄 버튼).

## 필요한 것

- Wayland GNOME. Fedora 44의 GNOME 50, 125% 배율 모니터 한 대에서 시험했습니다.
- 멀티 윈도우를 켠 Waydroid. 디스플레이 크기는 모니터의 논리 픽셀 크기로 맞춥니다.

  ```sh
  sudo waydroid prop set persist.waydroid.multi_windows true
  sudo waydroid prop set persist.waydroid.width 1536   # 1920x1200, 125%
  sudo waydroid prop set persist.waydroid.height 960
  ```

- `adb`. Waydroid 기본 주소 `192.168.240.112:5555`로 컨테이너에 붙습니다(`WLPROXY_ADB`로 바꿀 수
  있습니다). 모든 앱의 크기를 바꿀 수 있게 합니다:
  `adb shell settings put global force_resizable_activities 1`.
- Python 3(표준 라이브러리만)과, 트래픽을 해석할 프로토콜 파일: `wayland.xml`(Fedora는
  `wayland-devel`, Debian은 `libwayland-dev`)과 xdg-shell, viewporter, fractional-scale 파일
  (`wayland-protocols`, 또는 Fedora에서 `qt6-qtbase-gui`가 설치하는 Qt 6의 사본).

## 설치

```sh
git clone https://github.com/RedHatOnTop/waydroid-windows.git
cd waydroid-windows
./install.sh
waydroid session stop
```

`install.sh`는 사용자 서비스 `waydroid-wlproxy.service`와 `~/.local/bin`의 `waydroid` 래퍼를
설치합니다. 래퍼는 필요하면 서비스를 시작하고 Waydroid가 프록시 소켓을 쓰게 합니다. Waydroid의 앱
바로가기는 그냥 `waydroid`를 부르므로 앱 실행도 래퍼를 거칩니다. 세션이 프록시를 쓰면
`waydroid status`에 `Wayland display: .../wayland-wd`가 나오고, 로그는
`journalctl --user -u waydroid-wlproxy`에 있습니다. `./install.sh --remove`로 둘 다 지웁니다.

- 세션이 돌고 있을 때 서비스를 다시 시작하면 HWC의 연결이 끊깁니다. 세션을 먼저 멈추세요.
- 앱은 바로가기나 `waydroid app launch`로 여세요. `am start`로 열면 Waydroid의 실행 중 앱 목록이
  비어 있어서 HWC가 창을 아예 만들지 않습니다.

## 옵션

`wlproxy.py`의 환경 변수입니다. 서비스 파일에서 정합니다.

| 변수 | 기본값 | |
|---|---|---|
| `WLPROXY_ADB` | `192.168.240.112:5555` | 컨테이너의 adb 주소 |
| `WLPROXY_RING` | `12` | 크기 조절 테두리 두께(픽셀) |
| `WLPROXY_QUIET` | `0` | `1`이면 모든 트래픽 대신 창 단위 메시지와 프록시가 한 일만 기록(서비스에서 켬) |
| `WLPROXY_TASKD` | `1` | `0`이면 안드로이드 태스크를 건드리지 않음: 크기 조절·이동 없음 |
| `WLPROXY_GESTURES` | `1` | `0`이면 GNOME 쪽 이벤트에 반응하지 않음: 테두리, 제목 표시줄, Super 키 |
| `WLPROXY_GEOMETRY` | `1` | `0`이면 HWC의 요청을 그대로 넘김: 창 범위, 키보드 창 |
| `WLPROXY_REWRITE` | `1` | `0`이면 configure 크기를 그대로 넘김(핫플러그 포함) |
| `WLPROXY_TASK_DEX` | `wdtask/wdtask.dex` | 컨테이너에 넣을 WdTask 빌드 |
| `WLPROXY_HOSTCLIP` | `0` | `1`이면 이미지만 담긴 호스트 클립보드를 `content://compat.hostclip`을 제공하는 안드로이드 앱에 넘김(그 앱은 이 저장소에 없음) |

`WdTask.java`를 고친 뒤에는 `wdtask/build.sh`로 `wdtask.dex`를 다시 빌드합니다. JDK가 필요하고,
구글의 D8은 알아서 내려받습니다.

## 테스트

```sh
for t in tests/*_test.py; do python3 -I "$t"; done
```

실제 HWC에서 기록한 메시지 형태를 프록시에 넣고, 무엇을 넘기고 끼워 넣는지 확인합니다. Waydroid 없이
돌아갑니다. 프록시는 2026-10-08부터 만든 사람의 사용자 서비스로 돌고 있습니다. 끌기, 크기 조절,
최대화 검사는 중첩 GNOME 세션에서 돌렸는데, 그 하네스는 이 저장소에 없습니다.

## 한계

- HWC가 다시 만든 창은 새 위치에 나타납니다.
- 두 번째 모니터, 터치, 펜은 아직 써 보지 않았습니다.
- 팝업과 대화 상자는 아직 위치나 크기가 어색할 수 있습니다.
- 텍스트 클립보드는 양방향으로 되지만 Waydroid 창에 키보드 포커스가 있을 때만 됩니다. HWC도 일반
  Wayland 클라이언트라서 그때만 선택 영역을 받습니다.

## 라이선스

GPL-3.0-or-later.
