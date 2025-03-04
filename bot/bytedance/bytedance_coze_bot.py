# encoding:utf-8
import io
import os
from os.path import isfile
import requests
from urllib.parse import urlparse, unquote
from bot.bot import Bot
from bot.bytedance.coze_client import CozeClient
from bot.bytedance.coze_session import CozeSession, CozeSessionManager
from bridge.context import ContextType, Context
from bridge.reply import Reply, ReplyType
from common.log import logger
from config import conf
from common import memory
from common.utils import parse_markdown_text
from common.tmp_dir import TmpDir
from cozepy import MessageType, Message, MessageContentType, MessageRole

class ByteDanceCozeBot(Bot):
    def __init__(self):
        super().__init__()
        self.sessions = CozeSessionManager(CozeSession)
        self.coze_api_base = conf().get("coze_api_base", "https://api.coze.cn/")
        self.coze_api_key = conf().get('coze_api_key', '')
        if conf().get('coze_return_show_img', False):
            self.show_img_file = True
        else:
            self.show_img_file = False
        coze_bot_id = conf().get('coze_bot_id', '')
        coze_bot_id = str(coze_bot_id)
        if not coze_bot_id:
            logger.error("[COZE] coze_bot_id is not set")
            raise Exception("coze_bot_id is not set")
        self.coze_bot_id = coze_bot_id
        # 存储群组ID和conversation_id的映射
        self.group_conversations = {}

    def reply(self, query, context: Context = None):
        # acquire reply content
        if context.type == ContextType.TEXT or context.type == ContextType.IMAGE_CREATE:
            if context.type == ContextType.IMAGE_CREATE:
                query = conf().get('image_create_prefix', ['画'])[0] + query
            logger.info("[COZE] query={}".format(query))
            
            # 获取用户ID
            channel_type = conf().get("channel_type", "wx")
            user_id = None
            if channel_type in ["wx", "wework", "gewechat"]:
                user_id = context["msg"].other_user_id
                if user_id is None or user_id == '':
                    user_id = context["msg"].actual_user_id
            elif channel_type in ["wechatcom_app", "wechatmp", "wechatmp_service", "wechatcom_service"]:
                user_id = context["msg"].other_user_id
                if user_id is None or user_id == '':
                    user_id = "default"
            else:
                return Reply(ReplyType.ERROR, f"unsupported channel type: {channel_type}")
            
            logger.debug(f"[COZE] user_id={user_id}")
            session_id = context["session_id"]
            
            # 处理群聊
            group_id = None
            if context.get("isgroup", False):
                group_id = context["msg"].other_user_id
                if group_id not in self.group_conversations:
                    # 如果是新的群组,创建新的conversation
                    chat_client = CozeClient(self.coze_api_key, self.coze_api_base)
                    conversation = chat_client.coze.conversations.create()
                    self.group_conversations[group_id] = conversation.id
                    logger.info(f"[COZE] Created new conversation for group {group_id}: {conversation.id}")

            # 创建或获取会话，传入群组ID
            session = self.sessions.session_query(query, user_id, session_id, group_id)
            
            # 设置用户昵称
            if channel_type in ["wx", "wework", "gewechat"]:
                if context.get("isgroup", False):
                    # 群聊场景使用发消息者的昵称
                    nickname = context["msg"].actual_user_nickname
                else:
                    # 私聊场景使用对方的昵称
                    nickname = context["msg"].other_user_nickname
                if nickname is None or nickname == '':
                    nickname = "unknown"
                session.set_user_nickname(nickname)
            
            # 如果是群聊，设置conversation_id
            if group_id:
                session.set_conversation_id(self.group_conversations[group_id])
                logger.debug(f"[COZE] Using conversation_id {self.group_conversations[group_id]} for group {group_id}")

            logger.info(f"[COZE] session={session} query={query}")

            # 如果不需要回复，只记录消息
            if context.get("need_reply", True) is False:
                logger.debug("[COZE] message need not reply, just record")
                session.add_user_message(query)
                return None

            reply, err = self._reply(query, session, context)
            if err != None:
                error_msg = conf().get("error_reply", "我暂时遇到了一些问题，请您稍后重试~")
                reply = Reply(ReplyType.TEXT, error_msg)
            return reply
        else:
            reply = Reply(ReplyType.ERROR, "Bot不支持处理{}类型的消息".format(context.type))
            return reply

    def _reply(self, query, session: CozeSession, context: Context):
        chat_client = CozeClient(self.coze_api_key, self.coze_api_base)
        
        # 获取历史消息
        additional_messages = session.get_message_history()
        
        # 添加文件上传消息
        file_messages = self._get_upload_files(session)
        if file_messages:
            additional_messages.extend(file_messages)
            
        # 发送消息并获取回复
        messages = chat_client.create_chat_message(
            bot_id=self.coze_bot_id,
            query=query,
            additional_messages=additional_messages,
            session=session
        )
        
        if self.show_img_file:
            return self.get_parsed_reply(messages, context)
        else:
            return self.get_text_reply(messages, session)

    def get_parsed_reply(self, messages: list[Message], context: Context = None):
        parsed_content = None
        for message in messages:
            if message.type == MessageType.ANSWER:
                conte = parse_markdown_text(message.content)
                if parsed_content is None:
                    parsed_content = conte
                else:
                    parsed_content.append(conte)

            # {"answer": "![image](/files/tools/dbf9cd7c-2110-4383-9ba8-50d9fd1a4815.png?timestamp=1713970391&nonce=0d5badf2e39466042113a4ba9fd9bf83&sign=OVmdCxCEuEYwc9add3YNFFdUpn4VdFKgl84Cg54iLnU=)"}
        at_prefix = ""
        channel = context.get("channel")
        is_group = context.get("isgroup", False)
        if is_group:
            at_prefix = "@" + context["msg"].actual_user_nickname + "\n"
        for item in parsed_content[:-1]:
            reply = None
            if item['type'] == 'text':
                content = at_prefix + item['content']
                reply = Reply(ReplyType.TEXT, content)
            elif item['type'] == 'image':
                image_url = self._fill_file_base_url(item['content'])
                image = self._download_image(image_url)
                if image:
                    reply = Reply(ReplyType.IMAGE, image)
                else:
                    reply = Reply(ReplyType.TEXT, f"图片链接：{image_url}")
            elif item['type'] == 'file':
                file_url = self._fill_file_base_url(item['content'])
                if isfile(file_url):
                    file_path = self._download_file(file_url)
                    if file_path:
                        reply = Reply(ReplyType.FILE, file_path)
                else:
                    reply = Reply(ReplyType.TEXT, f"链接：{file_url}")
            logger.debug(f"[COZE] reply={reply}")
            if reply and channel:
                channel.send(reply, context)

        final_item = parsed_content[-1]
        final_reply = None
        if final_item['type'] == 'text':
            content = final_item['content']
            if is_group:
                at_prefix = "@" + context["msg"].actual_user_nickname + "\n"
                content = at_prefix + content
            final_reply = Reply(ReplyType.TEXT, final_item['content'])
        elif final_item['type'] == 'image':
            image_url = self._fill_file_base_url(final_item['content'])
            image = self._download_image(image_url)
            if image:
                final_reply = Reply(ReplyType.IMAGE, image)
            else:
                final_reply = Reply(ReplyType.TEXT, f"图片链接：{image_url}")
        elif final_item['type'] == 'file':
            file_url = self._fill_file_base_url(final_item['content'])
            if isfile(file_url):
                file_path = self._download_file(file_url)
                if file_path:
                    final_reply = Reply(ReplyType.FILE, file_path)
            else:
                final_reply = Reply(ReplyType.TEXT, f"链接：{file_url}")
        return final_reply, None

    # def _get_api_base_url(self):
    #     return conf().get("coze_api_base", "https://api.coze.cn/open_api/v2")

    def _get_completion_content(self, messages: list):
        answer = None
        for message in messages:
            if message.type == MessageType.ANSWER and message.content_type == MessageContentType.TEXT:
                answer = message.content
                break
        if not answer:
            return None, "[COZE] Error: empty answer"
        return answer, None

    def _calc_tokens(self, messages, answer):
        # 简单统计token
        completion_tokens = len(answer)
        prompt_tokens = 0
        for message in messages:
            prompt_tokens += len(message.content)
        return completion_tokens, prompt_tokens + completion_tokens

    def _get_upload_files(self, session: CozeSession):
        session_id = session.get_session_id()
        img_cache = memory.USER_IMAGE_CACHE.get(session_id)
        if not img_cache or not conf().get("image_recognition"):
            return None
        coze_client = CozeClient(self.coze_api_key, self.coze_api_base)
        msg = img_cache.get("msg")
        path = img_cache.get("path")
        msg.prepare()
        file = coze_client.file_upload(path)
        # 清理图片缓存
        memory.USER_IMAGE_CACHE[session_id] = None

        additional_messages = []
        additional_messages.append(coze_client.create_message(file))
        return additional_messages

    def _fill_file_base_url(self, url: str):
        if url.startswith("https://") or url.startswith("http://"):
            return url
        return self.coze_api_base + url

    def _download_image(self, url):
        try:
            pic_res = requests.get(url, stream=True)
            pic_res.raise_for_status()
            image_storage = io.BytesIO()
            size = 0
            for block in pic_res.iter_content(1024):
                size += len(block)
                image_storage.write(block)
            logger.debug(f"[WX] download image success, size={size}, img_url={url}")
            image_storage.seek(0)
            return image_storage
        except Exception as e:
            logger.error(f"Error downloading {url}: {e}")
        return None

    def _download_file(self, url):
        try:
            response = requests.get(url)
            response.raise_for_status()
            parsed_url = urlparse(url)
            logger.debug(f"Downloading file from {url}")
            url_path = unquote(parsed_url.path)
            # 从路径中提取文件名
            file_name = url_path.split('/')[-1]
            logger.debug(f"Saving file as {file_name}")
            file_path = os.path.join(TmpDir().path(), file_name)
            with open(file_path, 'wb') as file:
                file.write(response.content)
            return file_path
        except Exception as e:
            logger.error(f"Error downloading {url}: {e}")
        return None

    def get_text_reply(self, messages, session: CozeSession):
        answer, err = self._get_completion_content(messages)
        if err is not None:
            return None, err
        
        # 将助手的回复添加到历史记录
        session.add_assistant_message(answer)
        
        completion_tokens, total_tokens = self._calc_tokens(session.message_history, answer)

        # 处理图片URL
        import re
        image_urls = []
        # 匹配 image_url=http(s)://... 格式的URL
        pattern = r'image_url=(https?://[^\s]+)'
        matches = re.finditer(pattern, answer)
        
        # 收集所有图片URL并从文本中删除
        for match in matches:
            image_url = match.group(1)
            image_urls.append(image_url)
            # 删除匹配到的image_url部分
            answer = answer.replace(f'image_url={image_url}', '')
        
        # 发送文本回复
        text_reply = Reply(ReplyType.TEXT, answer.strip())

        # 如果有图片URL，下载并发送图片
        if image_urls:
            for image_url in image_urls:
                image = self._download_image(image_url)
                if image:
                    return Reply(ReplyType.IMAGE, image), None
        
        if err is not None:
            logger.error("[COZE] reply error={}".format(err))
            return Reply(ReplyType.ERROR, "我暂时遇到了一些问题，请您稍后重试~")
            
        logger.info(
            "[COZE] new_query={}, session_id={}, reply_cont={}, completion_tokens={}， image_urls={}".format(
                session.message_history,
                session.get_session_id(),
                answer,
                completion_tokens,
                image_urls
            )
        )
        return text_reply, None
