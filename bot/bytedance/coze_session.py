from common.expired_dict import ExpiredDict
from config import conf
from common.log import logger
from cozepy import Message, MessageRole, MessageContentType


class CozeSession(object):
    def __init__(self, session_id: str, user_id: str, group_id: str = None, conversation_id=None, system_prompt=None):
        self.__session_id = session_id
        self.__user_id = user_id
        self.__group_id = group_id  # 添加群组ID
        self.__conversation_id = conversation_id
        self.__user_message_counter = 0
        self.message_history = []  # 存储消息历史
        self.max_history_len = 10  # 最多保存10条消息
        if system_prompt is None:
            self.system_prompt = conf().get("character_desc", "")
        else:
            self.system_prompt = system_prompt

    def __str__(self):
        return f"CozeSession(session_id={self.__session_id}, user_id={self.__user_id}, group_id={self.__group_id}, conversation_id={self.__conversation_id}, messages_count={len(self.message_history)})"

    def get_group_id(self):
        """获取群组ID"""
        return self.__group_id

    def set_group_id(self, group_id: str):
        """设置群组ID"""
        self.__group_id = group_id

    def add_message(self, message: Message):
        """添加一条消息到历史记录"""
        if len(self.message_history) >= self.max_history_len:
            self.message_history.pop(0)  # 移除最旧的消息
        self.message_history.append(message)

    def add_user_message(self, content: str):
        """添加用户消息"""
        message = Message(
            role=MessageRole.USER,
            content=content,
            content_type=MessageContentType.TEXT
        )
        self.add_message(message)

    def add_assistant_message(self, content: str):
        """添加助手消息"""
        message = Message(
            role=MessageRole.ASSISTANT,
            content=content,
            content_type=MessageContentType.TEXT
        )
        self.add_message(message)

    def get_message_history(self) -> list[Message]:
        """获取消息历史"""
        return self.message_history

    def clear_history(self):
        """清空消息历史"""
        self.message_history = []

    def get_session_id(self):
        return self.__session_id

    def get_user_id(self):
        return self.__user_id

    def get_conversation_id(self):
        return self.__conversation_id

    def set_conversation_id(self, conversation_id):
        self.__conversation_id = conversation_id

    def count_user_message(self):
        if conf().get("coze_conversation_max_messages", 5) <= 0:
            # 当设置的最大消息数小于等于0，则不限制
            return
        if self.__user_message_counter >= conf().get("coze_conversation_max_messages", 5):
            self.__user_message_counter = 0
            # FIXME: coze目前不支持设置历史消息长度，暂时使用超过5条清空会话的策略，缺点是没有滑动窗口，会突然丢失历史消息
            self.__conversation_id = ''
            self.clear_history()  # 清空消息历史
        self.__user_message_counter += 1


class CozeSessionManager(object):
    def __init__(self, sessioncls, **session_args):
        if conf().get("expires_in_seconds"):
            sessions = ExpiredDict(conf().get("expires_in_seconds"))
        else:
            sessions = dict()
        self.sessions = sessions
        self.sessioncls = sessioncls
        self.session_args = session_args

    def _build_session(self, session_id: str, user_id: str, group_id: str = None, system_prompt=None):
        """
        构建会话，如果是群聊则使用 group_id 作为 key
        """
        key = f"{group_id}:{session_id}" if group_id else session_id
        
        if session_id is None:
            return self.sessioncls(session_id, user_id, group_id, system_prompt, **self.session_args)
            
        if key not in self.sessions:
            self.sessions[key] = self.sessioncls(session_id, user_id, group_id, system_prompt, **self.session_args)
        
        session = self.sessions[key]
        return session

    def session_query(self, query, user_id, session_id, group_id=None):
        """
        处理用户查询，支持群聊
        """
        session = self._build_session(session_id, user_id, group_id)
        session.add_user_message(query)
        return session

    def session_reply(self, reply, user_id, session_id, group_id=None, total_tokens=None):
        """
        处理助手回复，支持群聊
        """
        session = self._build_session(session_id, user_id, group_id)
        session.add_assistant_message(reply)
        try:
            max_tokens = conf().get("conversation_max_tokens", 1000)
            tokens_cnt = session.discard_exceeding(max_tokens, total_tokens)
            logger.debug("raw total_tokens={}, savesession tokens={}".format(total_tokens, tokens_cnt))
        except Exception as e:
            logger.warning("Exception when counting tokens precisely for session: {}".format(str(e)))
        return session

    def clear_session(self, session_id, group_id=None):
        """
        清除指定会话，支持群聊
        """
        key = f"{group_id}:{session_id}" if group_id else session_id
        if key in self.sessions:
            del self.sessions[key]

    def clear_all_session(self):
        """
        清除所有会话
        """
        self.sessions.clear()
