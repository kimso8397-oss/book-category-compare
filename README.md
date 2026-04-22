# 📚 도서 카테고리 비교

책 제목을 입력하면 **알라딘 · 교보문고 · 예스24** 세 서점에서
같은 책이 어떤 카테고리로 분류되어 있는지 한 번에 비교해서 보여주는 작은 웹앱이에요.

- API 키가 필요 없어요. 대신 파이썬 서버가 각 서점 사이트를 대신 열어봐서
  카테고리 텍스트를 긁어옵니다. (웹 스크래핑)
- 서점의 검색 결과 중 **가장 위에 나오는 책 1권**을 기준으로 보여줍니다.

## 폴더 구성

```
도서 카테고리 검색/
├── app.py            ← 파이썬 서버 (Flask)
├── index.html        ← 화면
├── requirements.txt  ← 설치할 패키지 목록
└── README.md         ← 지금 읽고 있는 이 파일
```

## 처음 한 번만 설정하기 (로컬 실행)

터미널(맥에서는 "터미널", 윈도우에서는 "명령 프롬프트")을 열고 이 폴더로 이동한 뒤:

```bash
# 1) 패키지 설치
pip install -r requirements.txt

# 2) 서버 실행
python app.py
```

실행되면 `http://localhost:5000` 주소로 사이트가 열립니다.
브라우저에서 그 주소로 접속해서 책 제목을 입력해 보세요.

> Mac에서 `pip: command not found` 가 뜨면 `pip3 install -r requirements.txt`,
> `python: command not found` 가 뜨면 `python3 app.py` 로 바꿔서 실행하세요.

## 친구한테 공유하려면? (배포)

`localhost`는 내 컴퓨터에서만 열리는 주소라서 친구는 못 들어와요.
친구도 쓰게 하려면 이 파이썬 서버를 인터넷 어딘가에 올려놓아야 합니다.
가장 쉬운 무료 방법 2가지:

### 방법 A. Render.com (추천, 파이썬 서버에 제일 맞음)

1. 이 폴더를 GitHub 저장소(public)로 올려둡니다.
2. [render.com](https://render.com) 가입 → **New → Web Service** 선택.
3. GitHub 저장소 연결 후 다음과 같이 입력:
   - **Environment**: `Python 3`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app` (아래 설명 참고)
4. 배포가 끝나면 `https://xxxx.onrender.com` 주소가 나와요. 그 주소를 친구한테 공유하면 끝!

`gunicorn`을 쓰려면 `requirements.txt`에 `gunicorn`을 한 줄 추가해 주세요.
(Flask 기본 개발 서버로 돌려도 동작은 하지만 Render 에서는 `gunicorn`이 권장됩니다.)

### 방법 B. Railway.app

비슷하게 GitHub 연결 후 "Deploy from repo" 하면 알아서 파이썬 프로젝트로 감지해서 배포해 줍니다.

## 어떻게 작동하나요? (간단 설명)

브라우저 혼자서는 CORS 라는 규칙 때문에 다른 사이트(알라딘 등)의 내용을 직접 못 가져와요.
그래서 중간에 **파이썬 서버**가 서서 대신 페이지를 열고 카테고리 글자만 뽑아서
브라우저한테 JSON 으로 돌려주는 구조예요.

각 서점에서 카테고리를 뽑는 방법:

| 서점 | 방법 |
|------|------|
| 알라딘 | 상세 페이지 안의 JSON-LD `"genre"` 필드 |
| 교보문고 | 상세 페이지의 `<ol class="breadcrumb_list">` 빵부스러기 내비게이션 |
| 예스24 | 상세 페이지의 "카테고리 분류" 섹션의 첫 번째 `<li>` |

세 사이트를 **동시에** 병렬로 부르기 때문에 한 번 검색에 보통 2~5초 정도 걸립니다.

## 주의사항

- 이 스크립트는 HTML 구조를 읽어서 분석하기 때문에, 각 서점이 화면 구조를 바꾸면
  카테고리가 안 나올 수 있어요. 그러면 `app.py` 의 정규식을 조금 손봐야 합니다.
- 검색량이 과하게 많아지면 서점 쪽에서 접속을 잠깐 막을 수 있으니
  친구 몇 명 정도 써보는 수준으로만 사용하세요.
