import os
import json
import time
import hashlib
import asyncio
import xml.etree.ElementTree as ET
from fastapi import FastAPI, Request, Response
from fastapi.responses import PlainTextResponse
import httpx
from Crypto.Cipher import AES
import base64

app = FastAPI()

# ============ 配置区 ============
WECOM_CORP_ID = os.environ.get("WECOM_CORP_ID", "")
WECOM_AGENT_ID = os.environ.get("WECOM_AGENT_ID", "")
WECOM_SECRET = os.environ.get("WECOM_SECRET", "")
WECOM_TOKEN = os.environ.get("WECOM_TOKEN", "")
WECOM_ENCODING_AES_KEY = os.environ.get("WECOM_ENCODING_AES_KEY", "")
COZE_BOT_ID = os.environ.get("COZE_BOT_ID", "")
COZE_ACCESS_TOKEN = os.environ.get("COZE_ACCESS_TOKEN", "")

# ============ 企微加解密 ============
class WeComCrypto:
    """企业微信消息加解密"""
    def __init__(self, token, encoding_aes_key, corp_id):
        self.token = token
        self.corp_id = corp_id
        # EncodingAESKey 加上等于号后 base64 解码
        self.aes_key = base64.b64decode(encoding_aes_key + "=")

    def check_signature(self, msg_signature, timestamp, nonce, echostr):
        """验证URL有效性"""
        sign_list = [self.token, timestamp, nonce, echostr]
        sign_list.sort()
        sha1 = hashlib.sha1("".join(sign_list).encode()).hexdigest()
        if sha6 != msg_signature:
            raise Exception("签名验证失败")
        
        # 解密echostr
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        plain = cipher.decrypt(base64.b64decode(echostr))
        # 去除补位
        pad = plain[-1]
        plain = plain[:-pad]
        # 去掉16字节随机字符串 + 4字节消息长度 + 消息内容 + corp_id
        xml_len = int.from_bytes(plain[16:20], 'big')
        return plain[20:20 + xml_len].decode('utf-8')

    def decrypt_message(self, msg_signature, timestamp, nonce, encrypt_text):
        """解密收到的消息"""
        # 验签
        sign_list = [self.token, timestamp, nonce, encrypt_text]
        sign_list.sort()
        sha1 = hashlib.sha1("".join(sign_list).encode()).hexdigest()
        if sha1 != msg_signature:
            raise Exception("签名验证失败")
        
        # 解密
        cipher = AES.new(self.aes_key, AES.MODE_CBC, self.aes_key[:16])
        plain = cipher.decrypt(base64.b64decode(encrypt_text))
        pad = plain[-1]
        plain = plain[:-pad]
        xml_len = int.from_bytes(plain[16:20], 'big')
        return plain[20:20 + xml_len].decode('utf-8')

# 实例化加解密对象（请确保环境变量已设置）
try:
    crypto = WeComCrypto(WECOM_TOKEN, WECOM_ENCODING_AES_KEY, WECOM_CORP_ID)
except Exception as e:
    print(f"加解密初始化失败，请检查 EncodingAESKey 格式: {e}")

# ============ 企微access_token缓存 ============
_token_cache = {"token": "", "expires_at": 0}

async def get_wecom_access_token():
    """获取企微access_token（带缓存）"""
    if _token_cache["token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
            params={
                "corpid": WECOM_CORP_ID,
                "corpsecret": WECOM_SECRET
            }
        )
        data = resp.json()
        if data.get("errcode") != 0:
            raise Exception(f"获取access_token失败: {data}")
        
        _token_cache["token"] = data["access_token"]
        _token_cache["expires_at"] = time.time() + data["expires_in"] - 300
        return _token_cache["token"]

# ============ 扣子API调用 ============
async def call_coze(user_message: str, user_id: str) -> str:
    """调用扣子API获取AI回复"""
    async with httpx.AsyncClient(timeout=120.0) as client:
        # 1. 创建对话
        resp = await client.post(
            "https://api.coze.cn/v3/chat",
            headers={
                "Authorization": f"Bearer {COZE_ACCESS_TOKEN}",
                "Content-Type": "application/json"
            },
            json={
                "bot_id": COZE_BOT_ID,
                "user_id": user_id,
                "stream": False,
                "auto_save_history": True,
                "additional_messages": [{
                    "role": "user",
                    "content": user_message,
                    "content_type": "text"
                }]
            }
        )
        
        data = resp.json()
        if data.get("code") != 0:
            return f"AI服务暂时不可用，请联系人工客服 180-6060-0598"
        
        chat_id = data["data"]["id"]
        conversation_id = data["data"]["conversation_id"]
        
        # 2. 轮询等待AI完成（最多等60秒）
        for _ in range(30):
            await asyncio.sleep(2)
            check = await client.get(
                "https://api.coze.cn/v3/chat/retrieve",
                params={
                    "chat_id": chat_id,
                    "conversation_id": conversation_id
                },
                headers={
                    "Authorization": f"Bearer {COZE_ACCESS_TOKEN}"
                }
            )
            status = check.json().get("data", {}).get("status", "")
            
            if status == "completed":
                # 3. 获取AI回复
                msg_resp = await client.get(
                    "https://api.coze.cn/v3/chat/message/list",
                    params={
                        "chat_id": chat_id,
                        "conversation_id": conversation_id
                    },
                    headers={
                        "Authorization": f"Bearer {COZE_ACCESS_TOKEN}"
                    }
                )
                messages = msg_resp.json().get("data", [])
                for msg in messages:
                    if msg.get("role") == "assistant" and msg.get("type") == "answer":
                        return msg["content"]
                return "AI已处理但未生成回复，请联系人工客服 180-6060-0598"
            
            elif status in ("failed", "requires_action"):
                return "AI处理遇到问题，请联系人工客服 180-6060-0598"
        
        return "AI响应超时，请稍后再试或联系人工客服 180-6060-0598"

# ============ 企微发消息 ============
async def send_wecom_message(user_id: str, content: str):
    """通过企微API主动发消息给用户"""
    try:
        token = await get_wecom_access_token()
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
                json={
                    "touser": user_id,
                    "msgtype": "text",
                    "agentid": int(WECOM_AGENT_ID),
                    "text": {"content": content}
                }
            )
            result = resp.json()
            if result.get("errcode") != 0:
                print(f"发送消息失败: {result}")
    except Exception as e:
        print(f"发送消息异常: {e}")

# ============ 路由 ============
# 1. GET 请求：用于企业微信服务器验证 (URL校验)
@app.get("/wx")
async def verify_url(request: Request):
    """企微URL验证"""
    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")
    echostr = request.query_params.get("echostr", "")
    
    if not all([msg_signature, timestamp, nonce, echostr]):
        return PlainTextResponse(content="Missing parameters", status_code=400)
        
    try:
        reply = crypto.check_signature(msg_signature, timestamp, nonce, echostr)
        return PlainTextResponse(content=reply)
    except Exception as e:
        print(f"验证失败: {e}")
        return PlainTextResponse(content="verification failed", status_code=403)

# 2. POST 请求：用于接收用户发送的消息
@app.post("/wx")
async def handle_message(request: Request):
    """处理企微消息"""
    msg_signature = request.query_params.get("msg_signature", "")
    timestamp = request.query_params.get("timestamp", "")
    nonce = request.query_params.get("nonce", "")
    
    body = await request.body()
    if not body:
        return PlainTextResponse(content="No body", status_code=400)
        
    try:
        # 尝试解析XML
        root = ET.fromstring(body)
        
        # 检查是否是加密消息 (有 Encrypt 节点)
        encrypt_node = root.find("Encrypt")
        if encrypt_node is not None:
            # 密文模式：解密
            encrypt = encrypt_node.text
            xml_content = crypto.decrypt_message(msg_signature, timestamp, nonce, encrypt)
            msg_root = ET.fromstring(xml_content)
        else:
            # 明文模式：直接使用
            msg_root = root

        msg_type = msg_root.find("MsgType").text
        from_user = msg_root.find("FromUserName").text
        
        if msg_type == "text":
            user_input = msg_root.find("Content").text
            # 异步处理并回复，立即返回 success 防止超时
            asyncio.create_task(process_and_reply(from_user, user_input))
            
        elif msg_type == "event":
            event = msg_root.find("Event").text
            if event == "enter_agent":
                # 用户进入应用
                asyncio.create_task(send_wecom_message(from_user, "您好，我是油站圈老柯！加油站买卖租信息随时问我~"))
                
        return PlainTextResponse(content="success")
        
    except Exception as e:
        print(f"处理消息异常: {e}")
        return PlainTextResponse(content="success")

# 异步处理函数
async def process_and_reply(user_id: str, user_message: str):
    """异步处理消息并回复"""
    try:
        ai_reply = await call_coze(user_message, user_id)
        await send_wecom_message(user_id, ai_reply)
    except Exception as e:
        print(f"处理回复异常: {e}")
        await send_wecom_message(user_id, "抱歉，系统暂时繁忙，请联系人工客服 180-6060-0598")

# ============ 健康检查 ============
@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "油站圈AI销售助手"}

# ============ 主动触达接口 ============
@app.post("/api/send-to-user")
async def send_to_user(request: Request):
    """主动给指定客户发消息"""
    body = await request.json()
    user_id = body.get("user_id", "")
    content = body.get("content", "")
    if not user_id or not content:
        return {"error": "user_id和content必填"}
    await send_wecom_message(user_id, content)
    return {"status": "sent", "user_id": user_id}

@app.post("/api/batch-send")
async def batch_send(request: Request):
    """批量发送消息"""
    body = await request.json()
    messages = body.get("messages", [])
    results = []
    for msg in messages:
        try:
            await send_wecom_message(msg["user_id"], msg["content"])
            results.append({"user_id": msg["user_id"], "status": "sent"})
            await asyncio.sleep(1)  # 避免频率限制
        except Exception as e:
            results.append({"user_id": msg["user_id"], "status": "failed", "error": str(e)})
    return {"results": results}

# ============ 启动 ============
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
