from typing import Any, Optional, Tuple, Type
from maubot import Plugin, MessageEvent
from maubot.handlers import command
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from mautrix.types import (ContentURI, RoomID, TextMessageEventContent, MessageType, Format)
from sqlalchemy import Column, String, Integer, Text, orm, or_
from sqlalchemy.ext.declarative import declarative_base
import aiohttp

class Config(BaseProxyConfig):
  def do_update(self, helper: ConfigUpdateHelper) -> None:
    helper.copy("openapi_key")

class MediaCache:
    __tablename__ = "media_cache"
    query: orm.Query = None

    openai_url: str = Column(String(255), primary_key=True)
    mxc_uri: ContentURI = Column(String(255))
    prompt: str = Column(String(255))
    file_name: str = Column(String(255))
    size: int = Column(Integer)

    def __init__(self, openai_url: str, mxc_uri: ContentURI, prompt: str, file_name: str, size: int) -> None:
        self.openai_url = openai_url
        self.mxc_uri = mxc_uri
        self.prompt = prompt
        self.file_name = file_name
        self.size = size

class DalleBot(Plugin):
  media_cache: Type[MediaCache]
  db: orm.Session

  async def start(self) -> None:
    await super().start()
    self.config.load_and_update()

    db_factory = orm.sessionmaker(bind=self.database)
    db_session = orm.scoped_session(db_factory)

    base = declarative_base()
    base.metadata.bind = self.database

    class MediaCacheImpl(MediaCache, base):
      query = db_session.query_property()

    self.media_cache = MediaCacheImpl
    base.metadata.create_all()
    self.db = db_session

  async def _get_media_info(self, image_url: str, prompt: str) -> Optional[MediaCache]:
      cache = self.media_cache.query.get(image_url)
      if cache is not None:
          return cache
      resp = await self.http.get(image_url)
      if resp.status == 200:
          data = await resp.read()
          file_name = image_url.split("/")[-1]
          uri = await self.client.upload_media(data)
          cache = self.media_cache(openai_url=image_url, mxc_uri=uri, prompt=prompt, 
                                   file_name=file_name, size=len(data))
          self.db.add(cache)
          self.db.commit()
          return cache
      else:
          self.log.error(f"Getting media info for {image_url} returned {resp.status}: "
                         f"{await resp.text()}")
          return None

  async def _openai_request(self, prompt: str):
    url = "https://api.openai.com/v1/images/generations" 
    headers = { "Authorization": f"Bearer {self.config['openapi_key']}" }
    async with aiohttp.ClientSession(headers=headers) as session:
      return await session.post(url, json={'prompt': prompt, 'n': 1, 'size': "1024x1024"})

  async def _image(self, room_id: RoomID, prompt: str) -> None:
    resp = await self._openai_request(prompt)
    images = await resp.json()
    #await self.client.send_message(room_id, TextMessageEventContent(body=str(images), msgtype=MessageType.TEXT))

    for image_resp in images["data"]:
        image = await self._get_media_info(image_resp["url"], prompt)
        if image is not None:
          content = TextMessageEventContent(
                  msgtype=MessageType.TEXT, format=Format.HTML,
                  external_url=image.openai_url,
                  body=f"**{prompt}**\n{image.mxc_uri}",
                  formatted_body=f"<strong>{prompt}</strong><br/>"
                                 f"<img src='{image.mxc_uri}' title='Dall-e 2 generated image'/>")
          await self.client.send_message(room_id, content)
        else:
          await self.client.send_message(room_id, TextMessageEventContent(body="Error getting image", 
                                                                          msgtype=MessageType.TEXT))

  def non_empty_string(x: str) -> Tuple[str, Any]:
      if not x:
          return x, None
      return "", x

  @command.new(name="image")
  @command.argument("query", pass_raw=True, required=True, parser=non_empty_string)
  async def image(self, evt: MessageEvent, query: str) -> None:
    await evt.mark_read()
    try:
      await self._image(evt.room_id, query)
    except Exception as e:
      await evt.reply("Error: " + str(e))

  @classmethod
  def get_config_class(cls) -> Type[BaseProxyConfig]:
    return Config

