# 一个空文件，告诉 Python 这个目录应该被认为是一个 Python 包。

# 必需要在工程的包中导入pymysql，并且要调用install_as_MySQLdb方法
import pymysql
# 这确保了在Django启动时Celery应用程序会被加载
from .celery import app as celery_app

# 调用此函数后，任何导入MySQLdb的应用程序会不知不觉地实际使用pymysql。
pymysql.install_as_MySQLdb()



__all__ = ('celery_app',)