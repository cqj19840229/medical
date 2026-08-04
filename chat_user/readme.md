安装依赖
pip install mysql-connector-python cryptography fastapi uvicorn
& 'C:\Users\56884\anaconda3\envs\medical\python.exe' -m uvicorn main:app --host 0.0.0.0 --port 8888 --app-dir C:\medical\github\medical\chat_user
nohup uvicorn main:app --host 0.0.0.0 --port 18888 > chat_user.log 2>&1 &
http://服务器IP:8000/docs