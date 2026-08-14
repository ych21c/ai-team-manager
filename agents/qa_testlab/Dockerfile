FROM ghcr.io/cirruslabs/flutter:3.41.6

# Firebase Test Lab 제출/결과 다운로드용 gcloud CLI + 결과 영상 재인코딩용 ffmpeg
# (Test Lab의 video.mp4는 실제로는 VP9라 모바일 Chrome에서 재생이 안 되는 문제가
# 있어서, 받은 뒤 H.264/AAC로 다시 인코딩해서 저장한다 — run.py의 _transcode_to_h264)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv curl git ca-certificates ffmpeg \
    && curl -sSL https://sdk.cloud.google.com | bash -s -- --disable-prompts --install-dir=/opt \
    && rm -rf /var/lib/apt/lists/*

# 이 이미지는 arm64(Apple Silicon) 네이티브인데 Android SDK의 aapt2는 x86_64
# 바이너리라, Rosetta가 번역을 시도해도 이미지 안에 x86_64용 동적 링커/런타임
# 라이브러리가 아예 없어 "failed to open elf at /lib64/ld-linux-x86-64.so.2"로
# 실행 자체가 안 됐다("AAPT2 Daemon startup failed"의 진짜 원인, counter-app에서
# 직접 재현/확인함 — 메모리나 /dev/shm 문제가 아니었다). amd64 멀티아크를 추가하고
# aapt2가 필요로 하는 최소 런타임(글리브c, libstdc++, zlib)을 x86_64로 깔아서
# Rosetta 번역이 실제로 성립하게 한다.
RUN dpkg --add-architecture amd64 \
    # 베이스 이미지의 기본 소스(ports.ubuntu.com)는 arm64 전용 미러라 amd64
    # 패키지가 없다 — 기존 소스는 arm64로 못박고, amd64용은 archive.ubuntu.com을
    # 따로 등록한다.
    && sed -i '/^Types: deb$/a Architectures: arm64' /etc/apt/sources.list.d/ubuntu.sources \
    && printf 'Types: deb\nURIs: http://archive.ubuntu.com/ubuntu/\nSuites: noble noble-updates noble-security noble-backports\nComponents: main universe restricted multiverse\nArchitectures: amd64\nSigned-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n' > /etc/apt/sources.list.d/amd64.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    libc6:amd64 libstdc++6:amd64 zlib1g:amd64 \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/google-cloud-sdk/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY run.py .

CMD ["python3", "run.py"]
