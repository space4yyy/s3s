# (ↄ) 2017-2024 eli fessler (frozenpandaman), clovervidia
# https://github.com/frozenpandaman/s3s
# License: GPLv3

import base64, hashlib, json, os, re, sys, time, urllib
import requests
from bs4 import BeautifulSoup

USE_OLD_NSOAPP_VER    = False # Change this to True if you're getting a "9403: Invalid token." error

S3S_VERSION           = "unknown"
NSOAPP_VERSION        = "unknown"
NSOAPP_VER_FALLBACK   = "2.10.1"
WEB_VIEW_VERSION      = "unknown"
WEB_VIEW_VER_FALLBACK = "10.0.0-88706e32" # fallback for current splatnet 3 ver
SPLATNET3_URL         = "https://api.lp1.av5ja.srv.nintendo.net"
GRAPHQL_URL           = SPLATNET3_URL + "/api/graphql"
F_GEN_URL             = "unknown"
NXAPI_AUTH_URL        = "https://nxapi-auth.fancy.org.uk/api/oauth/token"
NXAPI_AUTH_SCOPE      = "ca:gf ca:er ca:dr"
ZNC_URL               = "https://api-lp1.znc.srv.nintendo.net"
NXAPI_AUTH_CLIENT_ID  = ""
# Fixed compatibility identifier used by the upstream nxapi client.
NXAPI_CLIENT_VERSION  = "d8fAZDPzwimzQ7c6"
NXAPI_AUTH_TOKEN      = None
NXAPI_AUTH_EXPIRES_AT = 0
F_API_TIMEOUT_RETRIES = 2

# functions in this file & call stack:
# - get_nsoapp_version()
# - get_web_view_ver()
# - log_in() -> get_session_token()
# - get_gtoken() -> call_f_api()
# - get_bullet()
# - enter_tokens()

session = requests.Session()


def get_nxapi_auth_token():
	'''Gets and caches an OAuth access token for the nxapi f-generation API.'''

	global NXAPI_AUTH_TOKEN, NXAPI_AUTH_EXPIRES_AT
	if NXAPI_AUTH_TOKEN and time.time() < NXAPI_AUTH_EXPIRES_AT - 30:
		return NXAPI_AUTH_TOKEN

	client_id = NXAPI_AUTH_CLIENT_ID
	if not client_id:
		print("nxapi_client_id is not set in config.txt.")
		sys.exit(1)

	try:
		r = requests.post(
			NXAPI_AUTH_URL,
			headers={'Accept': 'application/json'},
			data={
				'grant_type': 'client_credentials',
				'client_id': client_id,
				'scope': NXAPI_AUTH_SCOPE,
			},
			timeout=30,
		)
		container = r.json()
		if not r.ok:
			print(f"Error obtaining nxapi-auth token (HTTP {r.status_code}):")
			print(json.dumps(container, indent=2, ensure_ascii=False))
			sys.exit(1)

		NXAPI_AUTH_TOKEN = container["access_token"]
		NXAPI_AUTH_EXPIRES_AT = time.time() + int(container.get("expires_in", 0))
		return NXAPI_AUTH_TOKEN
	except requests.exceptions.Timeout as exc:
		print("Could not obtain an nxapi-auth access token: request timed out after 30 seconds.")
		print(f"Details: {exc}")
		sys.exit(1)
	except requests.exceptions.RequestException as exc:
		print(f"Could not obtain an nxapi-auth access token: {type(exc).__name__}: {exc}")
		sys.exit(1)
	except (ValueError, KeyError, TypeError) as exc:
		print(f"Could not parse the nxapi-auth response: {type(exc).__name__}: {exc}")
		sys.exit(1)


def is_nxapi_f_url(url):
	'''Returns whether a f-generation URL is hosted by nxapi-znca-api.'''

	return url.startswith("https://nxapi-znca-api.fancy.org.uk/")


def nxapi_endpoint(f_gen_url, path):
	'''Builds an endpoint URL next to the configured nxapi f endpoint.'''

	return os.path.dirname(f_gen_url).rstrip('/') + '/' + path.lstrip('/')


def _report_nxapi_config_error(f_conf_url, error):
	'''Prints a useful, non-sensitive error for a failed nxapi config request.'''

	print("Could not determine the Nintendo Switch Online app version from nxapi.")
	if isinstance(error, requests.exceptions.Timeout):
		print(f"The nxapi config request timed out after 30 seconds: {f_conf_url}")
		return

	if isinstance(error, requests.exceptions.HTTPError):
		response = error.response
		status = response.status_code if response is not None else "unknown"
		print(f"The nxapi config request failed with HTTP {status}: {f_conf_url}")
		if response is not None:
			trace_id = response.headers.get('X-Trace-Id')
			content_type = response.headers.get('Content-Type')
			if trace_id:
				print(f"Trace ID: {trace_id}")
			if content_type:
				print(f"Response content type: {content_type}")
			try:
				response_data = response.json()
			except ValueError:
				response_data = None
			if isinstance(response_data, dict):
				for field in ('error', 'error_description', 'error_message', 'debug_id'):
					if field in response_data:
						print(f"{field}: {response_data[field]}")
			elif isinstance(status, int) and status >= 500:
				print("The nxapi service may be temporarily unavailable. Check https://nxapi-status.fancy.org.uk/")
		return

	if isinstance(error, requests.exceptions.RequestException):
		print(f"The nxapi config request failed: {type(error).__name__}: {error}")
		return

	if isinstance(error, json.JSONDecodeError):
		print("The nxapi config response was not valid JSON.")
		return

	if isinstance(error, KeyError):
		print(f"The nxapi config response is missing the required field: {error.args[0]}.")
		return

	if isinstance(error, (ValueError, TypeError)):
		print(f"The nxapi config response is invalid: {error}")
		return

	print(f"Unexpected error while reading nxapi config: {type(error).__name__}: {error}")


def post_coral_request(url, body, encrypted_body, nsoapp_version, coral_access_token=None):
	'''Sends a Coral request, using nxapi encryption when available.'''

	app_head = {
		'X-Platform':       'Android',
		'X-ProductVersion': nsoapp_version,
		'Content-Type':     'application/json; charset=utf-8',
		'Accept':           'application/json',
		'Accept-Encoding':  'gzip',
		'User-Agent':       f'com.nintendo.znca/{nsoapp_version}(Android/12)',
	}
	if coral_access_token:
		app_head['Authorization'] = f'Bearer {coral_access_token}'
	request_data = {'json': body}
	if encrypted_body is not None:
		app_head['Content-Type'] = 'application/octet-stream'
		app_head['Accept'] = 'application/octet-stream, application/json'
		request_data = {'data': encrypted_body}

	return requests.post(url, headers=app_head, timeout=60, **request_data)


def parse_coral_response(response, f_gen_url, encrypted=False):
	'''Parses a Coral response and decrypts it through nxapi when required.'''

	if not encrypted:
		return response.json()

	decrypt_body = {
		'data': base64.b64encode(response.content).decode('ascii')
	}
	decrypt_response = requests.post(
		nxapi_endpoint(f_gen_url, 'decrypt-response'),
		headers={
			'Authorization': f'Bearer {get_nxapi_auth_token()}',
			'Content-Type': 'application/json',
			'Accept': 'text/plain',
			'User-Agent': f's3s/{S3S_VERSION}',
		},
		json=decrypt_body,
		timeout=60,
	)
	decrypt_response.raise_for_status()
	decrypted_text = decrypt_response.text
	try:
		decrypted_json = decrypt_response.json()
		if isinstance(decrypted_json, dict) and isinstance(decrypted_json.get('data'), str):
			decrypted_text = decrypted_json['data']
	except ValueError:
		pass
	return json.loads(decrypted_text)


def call_coral_api_with_f(access_token, step, f_gen_url, user_id,
		coral_user_id, url, parameter, nsoapp_version):
	'''Generates f, sends a Coral request, and parses its response.'''

	f, uuid, timestamp, encrypted_body = call_f_api(
		access_token, step, f_gen_url, user_id, coral_user_id=coral_user_id,
		encrypt_url=url, encrypt_parameter=parameter
	)
	request_parameter = dict(parameter)
	request_parameter.update({'f': f, 'requestId': uuid, 'timestamp': timestamp})
	if coral_user_id is not None and 'registrationToken' in request_parameter:
		request_parameter['registrationToken'] = access_token
	body = {'parameter': request_parameter}
	response = post_coral_request(
		url, body, encrypted_body, nsoapp_version,
		coral_access_token=access_token if step == 2 else None
	)
	return parse_coral_response(response, f_gen_url, encrypted_body is not None)


def get_nsoapp_version():
	'''Fetches the current Nintendo Switch Online app version from f API or the Apple App Store and sets it globally.'''

	if USE_OLD_NSOAPP_VER:
		return NSOAPP_VER_FALLBACK

	global NSOAPP_VERSION
	if NSOAPP_VERSION != "unknown": # already set
		return NSOAPP_VERSION
	else:
		# should exist already - from log_in() or get_gtoken() - but check to make sure
		try:
			global S3S_VERSION, F_GEN_URL
			assert S3S_VERSION != "unknown"
			assert F_GEN_URL != "unknown"
		except AssertionError:
			print("Cannot determine s3s version or f generation API.")
			sys.exit(1)

		try: # try to get NSO version from f API
			f_conf_url = os.path.dirname(F_GEN_URL) + "/config" # default endpoint for imink API
			f_conf_header = {'User-Agent': f's3s/{S3S_VERSION}'}
			if is_nxapi_f_url(F_GEN_URL):
				f_conf_header['Authorization'] = f'Bearer {get_nxapi_auth_token()}'
			f_conf_rsp = requests.get(f_conf_url, headers=f_conf_header, timeout=30)
			f_conf_rsp.raise_for_status()
			f_conf_json = json.loads(f_conf_rsp.text)
			if not isinstance(f_conf_json, dict):
				raise TypeError("response root is not a JSON object")
			ver = f_conf_json["nso_version"]
			if not isinstance(ver, str) or not ver.strip():
				raise ValueError("nso_version is empty or has an invalid type")
			ver = ver.strip()

			NSOAPP_VERSION = ver

			return NSOAPP_VERSION
		except SystemExit:
			raise
		except Exception as exc: # fallback to apple app store
			if is_nxapi_f_url(F_GEN_URL):
				_report_nxapi_config_error(f_conf_url, exc)
				sys.exit(1)
			try:
				page = requests.get("https://apps.apple.com/us/app/nintendo-switch-online/id1234806557")
				soup = BeautifulSoup(page.text, 'html.parser')
				elt = soup.find("p", {"class": "whats-new__latest__version"})
				ver = elt.get_text().replace("Version ", "").strip()

				NSOAPP_VERSION = ver

				return NSOAPP_VERSION
			except: # error with web request
				pass
				
			return NSOAPP_VER_FALLBACK


def get_web_view_ver(bhead=[], gtoken=""):
	'''Finds & parses the SplatNet 3 main.js file to fetch the current site version and sets it globally.'''

	global WEB_VIEW_VERSION
	if WEB_VIEW_VERSION != "unknown":
		return WEB_VIEW_VERSION
	else:
		app_head = {
			'Upgrade-Insecure-Requests':   '1',
			'Accept':                      '*/*',
			'DNT':                         '1',
			'X-AppColorScheme':            'DARK',
			'X-Requested-With':            'com.nintendo.znca',
			'Sec-Fetch-Site':              'none',
			'Sec-Fetch-Mode':              'navigate',
			'Sec-Fetch-User':              '?1',
			'Sec-Fetch-Dest':              'document'
		}
		app_cookies = {
			'_dnt':    '1'     # Do Not Track
		}

		if bhead:
			app_head["User-Agent"]      = bhead.get("User-Agent")
			app_head["Accept-Encoding"] = bhead.get("Accept-Encoding")
			app_head["Accept-Language"] = bhead.get("Accept-Language")
		if gtoken:
			app_cookies["_gtoken"] = gtoken # X-GameWebToken

		try:
			home = requests.get(SPLATNET3_URL, headers=app_head, cookies=app_cookies)
		except requests.exceptions.ConnectionError:
				print("Could not connect to network. Please try again.")
				sys.exit(1)

		if home.status_code != 200:
			return WEB_VIEW_VER_FALLBACK

		soup = BeautifulSoup(home.text, "html.parser")
		main_js = soup.select_one("script[src*='static']")

		if not main_js: # failed to parse html for main.js file
			return WEB_VIEW_VER_FALLBACK

		main_js_url = SPLATNET3_URL + main_js.attrs["src"]

		app_head = {
			'Accept':              '*/*',
			'X-Requested-With':    'com.nintendo.znca',
			'Sec-Fetch-Site':      'same-origin',
			'Sec-Fetch-Mode':      'no-cors',
			'Sec-Fetch-Dest':      'script',
			'Referer':             SPLATNET3_URL # sending w/o lang, na_country, na_lang params
		}
		if bhead:
			app_head["User-Agent"]      = bhead.get("User-Agent")
			app_head["Accept-Encoding"] = bhead.get("Accept-Encoding")
			app_head["Accept-Language"] = bhead.get("Accept-Language")

		main_js_body = requests.get(main_js_url, headers=app_head, cookies=app_cookies)
		if main_js_body.status_code != 200:
			return WEB_VIEW_VER_FALLBACK

		pattern = r"\b(?P<revision>[0-9a-f]{40})\b[\S]*?void 0[\S]*?\"revision_info_not_set\"\}`,.*?=`(?P<version>\d+\.\d+\.\d+)-"
		match = re.search(pattern, main_js_body.text)
		if match is None:
			return WEB_VIEW_VER_FALLBACK

		version, revision = match.group("version"), match.group("revision")
		ver_string = f"{version}-{revision[:8]}"

		WEB_VIEW_VERSION = ver_string

		return WEB_VIEW_VERSION


def log_in(ver, app_user_agent, f_gen_url):
	'''Logs in to a Nintendo Account and returns a session_token.'''

	global S3S_VERSION, F_GEN_URL
	S3S_VERSION = ver
	F_GEN_URL = f_gen_url

	auth_state = base64.urlsafe_b64encode(os.urandom(36))

	auth_code_verifier = base64.urlsafe_b64encode(os.urandom(32))
	auth_cv_hash = hashlib.sha256()
	auth_cv_hash.update(auth_code_verifier.replace(b'=', b''))
	auth_code_challenge = base64.urlsafe_b64encode(auth_cv_hash.digest())

	app_head = {
		'Host':                      'accounts.nintendo.com',
		'Connection':                'keep-alive',
		'Cache-Control':             'max-age=0',
		'Upgrade-Insecure-Requests': '1',
		'User-Agent':                app_user_agent,
		'Accept':                    'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8n',
		'DNT':                       '1',
		'Accept-Encoding':           'gzip,deflate,br',
	}

	body = {
		'state':                               auth_state,
		'redirect_uri':                        'npf71b963c1b7b6d119://auth',
		'client_id':                           '71b963c1b7b6d119',
		'scope':                               'openid user user.birthday user.mii user.screenName',
		'response_type':                       'session_token_code',
		'session_token_code_challenge':        auth_code_challenge.replace(b'=', b''),
		'session_token_code_challenge_method': 'S256',
		'theme':                               'login_form'
	}

	print("\nMake sure you have read the \"Token generation\" section of the readme before proceeding. To manually input your tokens instead, enter \"skip\" at the prompt below.")
	print("\nNavigate to this URL in your browser:")
	print(f'https://accounts.nintendo.com/connect/1.0.0/authorize?{urllib.parse.urlencode(body)}')

	print("Log in, right click the \"Select this account\" button, copy the link address, and paste it below:")
	while True:
		try:
			use_account_url = input("")
			if use_account_url == "skip":
				return "skip"
			session_token_code = re.search('de=(.*)&st', use_account_url).group(1)
			return get_session_token(session_token_code, auth_code_verifier)
		except KeyboardInterrupt:
			print("\nBye!")
			sys.exit(1)
		except AttributeError:
			print("Malformed URL. Please try again, or press Ctrl+C to exit.")
			print("URL:", end=' ')


def get_session_token(session_token_code, auth_code_verifier):
	'''Helper function for log_in().'''

	nsoapp_version = get_nsoapp_version()

	app_head = {
		'User-Agent':      f'OnlineLounge/{nsoapp_version} NASDKAPI Android',
		'Accept-Language': 'en-US',
		'Accept':          'application/json',
		'Content-Type':    'application/x-www-form-urlencoded',
		'Content-Length':  '540',
		'Host':            'accounts.nintendo.com',
		'Connection':      'Keep-Alive',
		'Accept-Encoding': 'gzip'
	}

	body = {
		'client_id':                   '71b963c1b7b6d119',
		'session_token_code':          session_token_code,
		'session_token_code_verifier': auth_code_verifier.replace(b'=', b'')
	}

	url = 'https://accounts.nintendo.com/connect/1.0.0/api/session_token'
	r = session.post(url, headers=app_head, data=body)
	try:
		container = json.loads(r.text)
		s_t       = container["session_token"]
	except json.decoder.JSONDecodeError:
		print("Got non-JSON response from Nintendo (in api/session_token step). Please try again.")
		sys.exit(1)
	except KeyError:
		print("\nThe URL has expired. Logging out & back in to your Nintendo Account and retrying may fix this.")
		print("Error from Nintendo (in api/session_token step):")
		print(json.dumps(container, indent=2))
		sys.exit(1)

	return s_t


def get_gtoken(f_gen_url, session_token, ver):
	'''Provided the session_token, returns a GameWebToken JWT and account info.'''

	global S3S_VERSION, F_GEN_URL
	S3S_VERSION = ver
	F_GEN_URL = f_gen_url

	nsoapp_version = get_nsoapp_version()

	app_head = {
		'Host':            'accounts.nintendo.com',
		'Accept-Encoding': 'gzip',
		'Content-Type':    'application/json',
		'Content-Length':  '436',
		'Accept':          'application/json',
		'Connection':      'Keep-Alive',
		'User-Agent':      'Dalvik/2.1.0 (Linux; U; Android 14; Pixel 7a Build/UQ1A.240105.004)'
	}

	body = {
		'client_id':     '71b963c1b7b6d119',
		'session_token': session_token,
		'grant_type':    'urn:ietf:params:oauth:grant-type:jwt-bearer-session-token'
	}

	url = "https://accounts.nintendo.com/connect/1.0.0/api/token"
	r = requests.post(url, headers=app_head, json=body)
	try:
		id_response = json.loads(r.text)
	except json.decoder.JSONDecodeError:
		print("Got non-JSON response from Nintendo (in api/token step). Please try again.")
		sys.exit(1)

	# get user info
	try:
		app_head = {
			'User-Agent':      'NASDKAPI; Android',
			'Content-Type':    'application/json',
			'Accept':          'application/json',
			'Authorization':   f'Bearer {id_response["access_token"]}',
			'Host':            'api.accounts.nintendo.com',
			'Connection':      'Keep-Alive',
			'Accept-Encoding': 'gzip'
		}
	except:
		print("Not a valid authorization request. Please delete config.txt and try again.")
		print("Error from Nintendo (in api/token step):")
		print(json.dumps(id_response, indent=2))
		sys.exit(1)

	url = "https://api.accounts.nintendo.com/2.0.0/users/me"
	r = requests.get(url, headers=app_head)
	try:
		user_info = json.loads(r.text)
	except json.decoder.JSONDecodeError:
		print("Got non-JSON response from Nintendo (in users/me step). Please try again.")
		sys.exit(1)

	user_nickname = user_info["nickname"]
	user_lang     = user_info["language"]
	user_country  = user_info["country"]
	user_id       = user_info["id"]

	# get access token
	id_token = id_response["id_token"]
	login_url = ZNC_URL + '/v4/Account/Login'
	login_parameter = {
		'f':          '',
		'language':   user_lang,
		'naBirthday': user_info["birthday"],
		'naCountry':  user_country,
		'naIdToken':  id_token,
		'requestId':  '',
		'timestamp':  0
	}
	def request_login():
		return call_coral_api_with_f(
			id_token, 1, f_gen_url, user_id, None, login_url,
			login_parameter, nsoapp_version
		)

	try:
		splatoon_token = request_login()
	except SystemExit:
		raise
	except (json.decoder.JSONDecodeError, requests.RequestException, ValueError):
		print("Got non-JSON response from Nintendo (in Account/Login step). Please try again.")
		sys.exit(1)
	except:
		print("Error(s) from Nintendo:")
		print(json.dumps(id_response, indent=2))
		print(json.dumps(user_info, indent=2))
		sys.exit(1)

	try:
		access_token  = splatoon_token["result"]["webApiServerCredential"]["accessToken"]
		coral_user_id = str(splatoon_token["result"]["user"]["id"])
	except:
		# retry once if 9403/9599 error from nintendo
		try:
			splatoon_token = request_login()
			access_token  = splatoon_token["result"]["webApiServerCredential"]["accessToken"]
			coral_user_id = str(splatoon_token["result"]["user"]["id"])
		except:
			print("Error from Nintendo (in Account/Login step):")
			print(json.dumps(splatoon_token, indent=2))
			print("Try re-running the script. Or, if the NSO app has recently been updated, you may temporarily change `USE_OLD_NSOAPP_VER` to True at the top of iksm.py for a workaround.")
			sys.exit(1)

	# get web service token
	service_url = ZNC_URL + '/v4/Game/GetWebServiceToken'
	service_parameter = {
		'f':                 '',
		'id':                4834290508791808,
		'registrationToken': '',
		'requestId':         '',
		'timestamp':         0
	}
	def request_service_token():
		return call_coral_api_with_f(
			access_token, 2, f_gen_url, user_id, coral_user_id, service_url,
			service_parameter, nsoapp_version
		)

	try:
		web_service_resp = request_service_token()
	except (json.decoder.JSONDecodeError, requests.RequestException, ValueError):
		print("Got non-JSON response from Nintendo (in Game/GetWebServiceToken step). Please try again.")
		sys.exit(1)

	try:
		web_service_token = web_service_resp["result"]["accessToken"]
	except:
		# retry once if 9403/9599 error from nintendo
		try:
			web_service_resp = request_service_token()
			web_service_token = web_service_resp["result"]["accessToken"]
		except:
			print("Error from Nintendo (in Game/GetWebServiceToken step):")
			print(json.dumps(web_service_resp, indent=2))
			sys.exit(1)

	return web_service_token, user_nickname, user_lang, user_country


def get_bullet(web_service_token, app_user_agent, user_lang, user_country):
	'''Given a gtoken, returns a bulletToken.'''

	app_head = {
		'Content-Length':   '0',
		'Content-Type':     'application/json',
		'Accept-Language':  user_lang,
		'User-Agent':       app_user_agent,
		'X-Web-View-Ver':   get_web_view_ver(),
		'X-NACOUNTRY':      user_country,
		'Accept':           '*/*',
		'Origin':           SPLATNET3_URL,
		'X-Requested-With': 'com.nintendo.znca'
	}
	app_cookies = {
		'_gtoken': web_service_token, # X-GameWebToken
		'_dnt':    '1'                # Do Not Track
	}
	url = f'{SPLATNET3_URL}/api/bullet_tokens'
	r = requests.post(url, headers=app_head, cookies=app_cookies)

	if r.status_code == 401:
		print("Unauthorized error (ERROR_INVALID_GAME_WEB_TOKEN). Cannot fetch tokens at this time.")
		sys.exit(1)
	elif r.status_code == 403:
		print("Forbidden error (ERROR_OBSOLETE_VERSION). Cannot fetch tokens at this time.")
		sys.exit(1)
	elif r.status_code == 204: # No Content, USER_NOT_REGISTERED
		print("Cannot access SplatNet 3 without having played online.")
		sys.exit(1)

	try:
		bullet_resp = json.loads(r.text)
		bullet_token = bullet_resp["bulletToken"]
	except (json.decoder.JSONDecodeError, TypeError):
		print("Got non-JSON response from Nintendo (in api/bullet_tokens step):")
		print(r.text)
		bullet_token = ""
	except:
		print("Error from Nintendo (in api/bullet_tokens step):")
		print(json.dumps(bullet_resp, indent=2))
		sys.exit(1)

	return bullet_token


def call_f_api(access_token, step, f_gen_url, user_id, coral_user_id=None,
		encrypt_url=None, encrypt_parameter=None):
	'''Gets f data and optionally an encrypted Coral request body from the f API.'''

	try:
		nsoapp_version = get_nsoapp_version()
		api_head = {
			'User-Agent':      f's3s/{S3S_VERSION}',
			'Content-Type':    'application/json; charset=utf-8',
			'Accept':          'application/json',
			'X-znca-Platform': 'Android',
			'X-znca-Version':  nsoapp_version
		}
		if is_nxapi_f_url(f_gen_url):
			api_head['Authorization'] = f'Bearer {get_nxapi_auth_token()}'
			api_head['X-znca-Client-Version'] = NXAPI_CLIENT_VERSION
		api_body = { # 'timestamp' & 'request_id' (uuid v4) set automatically
			'token':       access_token,
			'hash_method': step, # 1 = coral (NSO) token, 2 = webservicetoken
			'na_id':       user_id
		}
		if step == 2 and coral_user_id is not None:
			api_body["coral_user_id"] = coral_user_id
		if is_nxapi_f_url(f_gen_url) and encrypt_url and encrypt_parameter is not None:
			api_body["encrypt_token_request"] = {
				"url": encrypt_url,
				"parameter": encrypt_parameter
			}

		for attempt in range(F_API_TIMEOUT_RETRIES + 1):
			api_response = requests.post(f_gen_url, data=json.dumps(api_body), headers=api_head, timeout=60)
			resp = json.loads(api_response.text)
			if resp.get('error') != 'timeout' or attempt == F_API_TIMEOUT_RETRIES:
				break
			time.sleep(2 ** (attempt + 1))

		f = resp["f"]
		uuid = resp["request_id"]
		timestamp = resp["timestamp"]
		encrypted_body = None
		if is_nxapi_f_url(f_gen_url) and resp.get("encrypted_token_request"):
			encrypted_data = resp["encrypted_token_request"]
			encrypted_data += '=' * (-len(encrypted_data) % 4)
			encrypted_body = base64.urlsafe_b64decode(encrypted_data)
		if encrypt_url is not None:
			return f, uuid, timestamp, encrypted_body
		return f, uuid, timestamp
	except SystemExit:
		raise
	except:
		try: # if api_response never gets set
			if api_response.text:
				print(f"Error during f generation:\n{json.dumps(json.loads(api_response.text), indent=2, ensure_ascii=False)}")
			else:
				print(f"Error during f generation: Error {api_response.status_code}.")
		except:
			print(f"Couldn't connect to f generation API ({f_gen_url}). Please try again later.")
		sys.exit(1)


def enter_tokens():
	'''Prompts the user to enter a gtoken and bulletToken.'''

	print("Go to the page below to find instructions to obtain your gtoken and bulletToken:")
	print("https://github.com/frozenpandaman/s3s/wiki/mitmproxy-instructions\n")

	new_gtoken = input("Enter your gtoken: ")
	while len(new_gtoken) != 926:
		new_gtoken = input("Invalid token - length should be 926 characters. Try again.\nEnter your gtoken: ")

	new_bullettoken = input("Enter your bulletToken: ")
	while len(new_bullettoken) != 124:
		if len(new_bullettoken) == 123 and new_bullettoken[-1] != "=":
			new_bullettoken += "=" # add a = to the end, which was probably left off (even though it works without)
		else:
			new_bullettoken = input("Invalid token - length should be 124 characters. Try again.\nEnter your bulletToken: ")

	return new_gtoken, new_bullettoken


if __name__ == "__main__":
	print("This program cannot be run alone. See https://github.com/frozenpandaman/s3s")
	sys.exit(0)
