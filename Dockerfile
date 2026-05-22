FROM python:3.10-slim

WORKDIR /app

# Cài đặt curl để hỗ trợ healthcheck nếu cần
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Cài đặt các thư viện phụ thuộc
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Sao chép mã nguồn chính
COPY app.py .
COPY main.py .

# Tạo thư mục lưu trữ file
RUN mkdir -p storage

# Cấu hình biến môi trường mặc định
ENV PORT=8000
ENV RETENTION_DAYS=7
ENV CLEANUP_INTERVAL_SECONDS=3600
ENV STORAGE_ROOT=/app/storage

# Mở cổng 8000
EXPOSE 8000

# Khởi chạy ứng dụng
CMD ["python", "app.py"]
