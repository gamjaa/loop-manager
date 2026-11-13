"""재구현에서 사용하는 작은 실행·Provider 설정."""
import configparser
from pathlib import Path


class Settings:
    def __init__(self,path):
        self.path = Path(path)
        self.values = configparser.ConfigParser(interpolation=None)
        if not self.values.read(self.path,encoding='utf-8'):
            raise ValueError(f'설정 파일을 읽을 수 없습니다: {self.path}')
        self.max_ai = self.values.getint('loop','max_workers')
        self.verify_timeout = self.values.getint('loop','verify_timeout_seconds')
        self.agent_timeout = 3600
        if self.max_ai<1 or self.verify_timeout<1:
            raise ValueError('AI 한도와 Verify timeout은 양수여야 합니다.')
        self.telegram_token = self.values.get('telegram','token',fallback='').strip()
        self.telegram_chat = self.values.get('telegram','chat_id',fallback='').strip()
        if bool(self.telegram_token)!=bool(self.telegram_chat):
            raise ValueError('Telegram token과 chat_id를 함께 설정하세요.')

    def fast(self,provider):
        value = self.values.get(provider,'fast_mode',fallback='').strip()
        return self.values.getboolean(provider,'fast_mode') if value else False

    def profile(self,provider,role,complexity,risk):
        if role=='worker':
            level = {'low':1,'medium':3,'high':5}[complexity]+(risk=='critical')
            configured = self.values.get(f'{provider}.worker',f'level{level}')
        else:
            configured = self.values.get(f'{provider}.roles',role)
        parts = [part.strip() for part in configured.split(',')]
        if len(parts)!=2 or not all(parts):
            raise ValueError(f'잘못된 {provider}/{role} model,effort 설정입니다.')
        alias,effort = parts
        model = self.values.get(f'{provider}.models',alias)
        return model,effort
