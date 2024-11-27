import requests
import hashlib
import base64
import hmac
import time
from openai import OpenAI
from datetime import datetime
import schedule
import time
import logging
from logging.handlers import RotatingFileHandler
import yaml

# 读取YAML配置文件
def init():
    with open('config.yaml', 'r') as stream:
        try:
            config = yaml.safe_load(stream)
            return config
        except yaml.YAMLError as ex:
            print("配置文件错误", ex)

# 创建一个logger
logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    filename="issues.log",  # 日志文件名
                    filemode='a')  

logger = logging.getLogger('GithubIssuesLog')

# 创建一个handler，用于写入日志文件，最大1MB，最多保留3个备份
handler = RotatingFileHandler("issues.log", maxBytes=1*1024*1024, backupCount=2)
logger.setLevel(logging.DEBUG)  # 设置日志级别

# 接收 github 的 issues
def jieshou_github(config):
    github_token = config['github']['token']
    # 设置请求头
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {github_token}",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    # github repo 链接 
    url = config['github']['repo']

    # 返回的内容
    result = []
    try:
        for i, u in enumerate(url):
            # 发送GET请求
            full_url = "https://api.github.com/repos/%s/issues" % u

            response = requests.get(full_url, headers=headers, allow_redirects=True)

            # 检查响应状态码
            if response.status_code == 200:
                # 解析JSON响应
                data = response.json()
                # 指定保存文件的名称
                filename = u.replace('/','_') + ".txt"

                with open(filename, 'r') as f:
                    nums = [line.strip() for line in f if line.strip()]
                
                for item in data:
                    item['repository'] = u
                    if str(item['number']) not in nums and item['state'] == 'open':
                        # 将数据写入文件
                        with open(filename, 'a', encoding='utf-8') as file:
                            file.write(str(item['number'])+'\n')
                        logger.info(f"接收到{u}仓库的issue{str(item['number'])}")
                        if "issues" in item["html_url"] and int(item['number']) > max(map(int,nums)):
                            # 返回的内容
                            result.append(item)
            time.sleep(6)
            
    except Exception as e:
        # 打印错误信息
        logger.error(f"接收 github issues 错误 --{e}")

    return result
    

# 计算飞书 robot 签名
def gen_sign(timestamp, secret):
    # 拼接timestamp和secret
    string_to_sign = '{}\n{}'.format(timestamp, secret)
    hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    # 对结果进行base64处理
    sign = base64.b64encode(hmac_code).decode('utf-8')
    return sign

# 推送消息至飞书 robot
def tuisong(config, template):
    timestamp = int(time.time())
    headers = {
        'Content-Type': 'application/json'
    }
    template['timestamp'] = timestamp
    template['sign'] = gen_sign(timestamp, config['feishu']['card_sign'])

    response = requests.post(url=config['feishu']['robot'], headers=headers, json=template)

    return response

# 使用大模型获得内容总结
def simple_chat(config, title, content):
    try:
        client = OpenAI(api_key=config['llm']['api_key'], base_url=config['llm']['base_url'])
        messages = [
            {
                "role": "user",
                "content": f"这是在github中开发者提出的issue的标题和内容，请根据内容进行总结，输出100-200字\ntitle:{title}\n content:{content}\n",
            }
        ]
        response = client.chat.completions.create(
            model=config['llm']['model'],
            messages=messages,
            stream=False,
            max_tokens=256,
            temperature=0.4,
            presence_penalty=1.2,
            top_p=0.8,
        )
        if response:
            return response.choices[0].message.content
    except Exception as e:
        logger.error(f"使用大模型获取内容总结错误--{e}")


# 处理最后推送的内容
def procress_messages(config, data):
    
    messages = []
    for item in data:
        template = {
            "msg_type":"interactive",
            "card":{
                "type":"template",
                "data":{
                    "template_id": config['feishu']['card_template_id'],
                    "template_version_name": config['feishu']['card_template_version_name'],
                    "template_variable": {
                        "repo": "",
                        "issues_id": "",
                        "issues": "",
                        "content": "",
                        "issues_url": ""
                    }
                }
            }
        }
        logger.info(f"正在处理推送: 仓库: {item['repository']}-- issue: {item['number']}")
        content = simple_chat(config, item['title'], item['body'])
        try:
            template["card"]['data']['template_variable']['repo'] = item['repository']
            template["card"]['data']['template_variable']['issues_id'] = item['number']
            template["card"]['data']['template_variable']['issues'] = item['title'] + '\n'
            template["card"]['data']['template_variable']['content'] = content + '\n'
            template["card"]['data']['template_variable']['issues_url'] = item['html_url'] + '\n'
            messages.append(template)
        except Exception as e:
            logger.error(f"处理推送错误--{e}")
        finally:
            time.sleep(10)
    return messages

# 主程序
def job():
    logger.info(f"Start preparing to push content")
    config = init()

    data = jieshou_github(config)
    if data:
        messages = procress_messages(config, data)
    else:
        return
    
    try:
        for temp in messages:
            response = tuisong(config, temp)
            if response.status_code == 200 and (response.json())['msg'] == 'success':
                issues_id = temp["card"]['data']['template_variable']['issues_id']
                repo_name = temp["card"]['data']['template_variable']['repo']
                logger.info(f"Success Push 「{repo_name}」 Issues 「{issues_id}」")
            else:
                logger.error("推送内容至飞书错误")
            time.sleep(60)
            
    except Exception as e:
        logger.error(f"ERROR: {e}")

# 勿扰时间设置
def run_job_if_time_is_appropriate():
    time_config = init()
    current_time = datetime.now().time()
    start_time = datetime.strptime(time_config['time']['start_time'], "%H:%M").time()
    end_time = datetime.strptime(time_config['time']['end_time'], "%H:%M").time()
    # 如果当前时间在 9 点到 21 点之间，执行 job
    if start_time <= current_time and current_time < end_time:
        job()
    return

if __name__ == "__main__":

    time_config = init()
    # 每小时执行一次
    schedule.every(time_config['time']['interval']).minutes.do(run_job_if_time_is_appropriate)

    while True:
        schedule.run_pending()
        time.sleep(1)
