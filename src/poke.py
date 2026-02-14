import requests
import os
from dotenv import load_dotenv

load_dotenv()


def send_poke_message(message: str) -> dict:
    api_key = os.getenv('POKE_API_KEY')
    if not api_key:
        raise ValueError('POKE_API_KEY is not set')

    response = requests.post(
        'https://poke.com/api/v1/inbound-sms/webhook',
        headers={
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        },
        json={'message': message}
    )
    response.raise_for_status()
    return response.json()


if __name__ == '__main__':
    print(send_poke_message('This is a test message from the Poke API'))
