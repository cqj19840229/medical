"""MySQL connection configuration."""

DB_CONFIG = {
    "host": "36.151.241.14",
    "port": 3306,
    "user": "root",
    "password": "G3-6OnWS1J7Kbf!9",
    "database": "fda_test",
    "charset": "utf8mb4",
}

MINIO_CONFIG = {
    "endpoint": "http://36.151.241.14:9002",
    "access_key": "minioadmin",
    "secret_key": "Minio@123456",
    "bucket_name": "medicalnew",
    "secure": False,
}
