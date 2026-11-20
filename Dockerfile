FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY reccessary_mcp.py .
# 建議部署時用環境變數 MCP_PATH 覆蓋成不易猜的路徑，例如 /mcp-x7k2q9
ENV MCP_PATH=/mcp
EXPOSE 8000
CMD ["python", "reccessary_mcp.py"]
