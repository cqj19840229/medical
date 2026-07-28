import os


DB_CONFIG = {
    "host": os.getenv("MYSQL_HOST", "36.151.241.14"),
    "port": int(os.getenv("MYSQL_PORT", "3306")),
    "user": os.getenv("MYSQL_USER", "root"),
    "password": os.getenv("MYSQL_PASSWORD", "G3-6OnWS1J7Kbf!9"),
    "database": os.getenv("MYSQL_DATABASE", "fda_test"),
    "charset": "utf8mb4",
}

NEO4J_CONFIG = {
    "uri": os.getenv("NEO4J_URI", "bolt://36.151.241.14:7687"),
    "user": os.getenv("NEO4J_USER", "neo4j"),
    "password": os.getenv("NEO4J_PASSWORD", "test123456"),
    "database": os.getenv("NEO4J_DATABASE", "neo4j"),
}

