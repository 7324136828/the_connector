### Genesis

I want you to build a chatGPT like application but allow different providers support. 

Based on the skills, can you create based on the original-project so that it will have everything in the root? 

Make sure you follow the guidance of productionization. 

Also make sure we can have a UI in reactjs such that the user can use chat functionality and select their own selector (kind of like chatGPT or config.json based connectors).


Create a backend with the python code based on original-project. Make sure you have setup.bat/setup.sh file that will dispatch setup.py such that it will setup virtual environment and run.bat/run.sh such that it will dispatch the frontend and backend? Your backend should have the following endpoint:

POST api/new --- establish a new chat session where the user is select with specific config.

POST api/chat -- which takes the user prompt and send via specific routing logic either the user provided. (you should keep a small context windows for these tasks unless user toggle past memory as false)

DELETE api/close -- end a session.

Any endpoint for agentic support will be greatly appreciated. As I will use for other project as a plugin as well.


Ignore any functionality regarding translate (as this is from a different project not for this project).

### Mixed requirement 1 


I want you to implement the following: (1)Can you remove all the sample chats at the landing? (2) fix the markdown display (3) rather than rely on the pre-configured config.json, you should allow the user to select their own config.json. (which can be passed via /api/sessions) (4) allow selection of efforts (if model provide such section, you should also allow the user to select), this should be applied to config.json as well (by default config.json is only easy in effort). (5) make sessions to be only based on config.json. (in addition to that, user can download a config.json example). (6) It seems that the past memory archiving is not working: **You**
What do you remember from past conversations?
**AI**
**Assistant**via **openai** (gpt-4o-mini)• 1325ms• 1378 tokensCopy
I don’t have the ability to remember past conversations or retain any personal data about users. Each interaction is independent, and I don’t have memory of previous chats. This design is intentional to prioritize user privacy and security. If you have any questions or topics you'd like to discuss, feel free to ask!


### Mixed requirement 2

Can you give a run script that only runs the backend? (make default port 8301) Also, can you give a detailed readme.md to support what are the api endpoints and how to use them etc.? How to use hermes agent to connect to it etc.? (make sure give hermes agent a list of configurations that are active as models)? This requires config management. Maybe have a separate screen or modal windows for the list of config.json and user can either upload a config.json or maybe use existing config.json configuration (from database)? the following apis are returning empty responses for agentic tasks, can we implement them?
GET /api/v1/models HTTP/1.1
GET /api/v1/models/gpt-4o-mini HTTP/1.1
POST /api/api/show HTTP/1.1
POST /api/chat/completions HTTP/1.1
GET / HTTP/1.1
GET /favicon.ico HTTP/1.1
GET /api/api/tags HTTP/1.1
GET /api/props HTTP/1.1
GET /api/v1/props HTTP/1.1
GET /api/version HTTP/1.1