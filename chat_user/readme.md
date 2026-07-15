安装依赖
pip install mysql-connector-python cryptography fastapi uvicorn
nohup uvicorn main:app --host 0.0.0.0 --port 8000 > chat_user.log 2>&1 &
http://服务器IP:8000/docs