from typing import Any

import httpx

from app.core.config import settings
from app.utils.text_cleaner import clean_surrogates


class EvolutionAPIService:
    def __init__(self):
        self.base_url = settings.evolution_api_base_url.rstrip('/')

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {'Content-Type': 'application/json'}
        if settings.evolution_api_key:
            headers['apikey'] = settings.evolution_api_key
        return headers

    async def get_information(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=settings.evolution_api_timeout) as client:
            response = await client.get(f'{self.base_url}/', headers=self._headers())
            response.raise_for_status()
            return response.json()

    async def set_instance_rabbitmq(self, instance_name: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=settings.evolution_api_timeout) as client:
            response = await client.post(
                f'{self.base_url}/rabbitmq/set/{instance_name}',
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return None

    async def send_text_message(self, instance_name: str, phone_number: str, text: str) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=settings.evolution_api_timeout) as client:
            response = await client.post(
                f'{self.base_url}/message/sendText/{instance_name}',
                json={
                    'number': phone_number,
                    'text': text,
                    'delay': 500,
                },
                headers=self._headers(),
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return None

    async def send_buttons(
        self,
        instance_name: str,
        phone_number: str,
        title: str,
        description: str,
        buttons: list[dict[str, str]],
        footer: str | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            'number': phone_number,
            'title': title,
            'description': description,
            'buttons': buttons,
            'delay': 500,
        }
        if footer:
            payload['footer'] = footer

        async with httpx.AsyncClient(timeout=settings.evolution_api_timeout) as client:
            response = await client.post(
                f'{self.base_url}/message/sendButtons/{instance_name}',
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return None

    async def send_list(
        self,
        instance_name: str,
        phone_number: str,
        title: str,
        description: str,
        button_text: str,
        sections: list[dict[str, Any]],
        footer: str | None = None,
    ) -> dict[str, Any] | None:
        payload: dict[str, Any] = {
            'number': phone_number,
            'title': title,
            'description': description,
            'buttonText': button_text,
            'sections': sections,
            'delay': 500,
        }
        if footer:
            payload['footerText'] = footer

        async with httpx.AsyncClient(timeout=settings.evolution_api_timeout) as client:
            response = await client.post(
                f'{self.base_url}/message/sendList/{instance_name}',
                json=payload,
                headers=self._headers(),
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return None

    def _clean_payload(self, data: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, str):
                result[key] = clean_surrogates(value)
            elif isinstance(value, dict):
                result[key] = self._clean_payload(value)
            elif isinstance(value, list):
                result[key] = [self._clean_payload(v) if isinstance(v, dict) else clean_surrogates(v) if isinstance(v, str) else v for v in value]
            else:
                result[key] = value
        return result

    async def send_message(
        self, instance_name: str, phone_number: str, reply: dict[str, Any]
    ) -> dict[str, Any] | None:
        reply = self._clean_payload(reply)
        msg_type = reply.get('type', 'text')

        if msg_type == 'buttons':
            return await self.send_buttons(
                instance_name=instance_name,
                phone_number=phone_number,
                title=reply.get('title', ''),
                description=reply.get('text', ''),
                buttons=reply.get('buttons', []),
                footer=reply.get('footer'),
            )

        if msg_type == 'list':
            return await self.send_list(
                instance_name=instance_name,
                phone_number=phone_number,
                title=reply.get('title', ''),
                description=reply.get('text', ''),
                button_text=reply.get('button_text', 'Select'),
                sections=reply.get('sections', []),
                footer=reply.get('footer'),
            )

        return await self.send_text_message(instance_name, phone_number, reply.get('text', ''))

    async def set_instance_webhook(self, instance_name: str, webhook_url: str) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=settings.evolution_api_timeout) as client:
            response = await client.post(
                f'{self.base_url}/webhook/set/{instance_name}',
                json={
                    'url': webhook_url,
                    'enabled': True,
                    'events': [
                        'MESSAGES_UPSERT',
                        'CONNECTION_UPDATE',
                        'QRCODE_UPDATED',
                    ],
                },
                headers=self._headers(),
            )
            response.raise_for_status()
            if response.content:
                return response.json()
            return None
