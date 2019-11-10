import demistomock as demisto
from CommonServerPython import *
from CommonServerUserPython import *

''' IMPORTS '''
import requests
import base64
import os
import json
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

# Disable insecure warnings
requests.packages.urllib3.disable_warnings()

''' GLOBAL VARS '''

DATE_FORMAT = '%Y-%m-%dT%H:%M:%SZ'

# Well known folders shortcut in MS Graph API
# For more information: https://docs.microsoft.com/en-us/graph/api/resources/mailfolder?view=graph-rest-1.0
WELL_KNOWN_FOLDERS = {
    'archive': 'archive',
    'conversation history': 'conversationhistory',
    'deleted items': 'deleteditems',
    'drafts': 'drafts',
    'inbox': 'inbox',
    'junk email': 'junkemail',
    'outbox': 'outbox',
    'sent items': 'sentitems',
}

EMAIL_DATA_MAPPING = {
    'id': 'ID',
    'createdDateTime': 'CreatedTime',
    'lastModifiedDateTime': 'ModifiedTime',
    'receivedDateTime': 'ReceivedTime',
    'sentDateTime': 'SentTime',
    'subject': 'Subject',
    'importance': 'Importance',
    'conversationId': 'ConversationID',
    'isRead': 'IsRead',
    'isDraft': 'IsDraft',
    'internetMessageId': 'MessageID'
}

''' HELPER FUNCTIONS '''


def epoch_seconds(d=None):
    """
    Return the number of seconds for given date. If no date, return current.

    :type d: ``datetime``
    :param d: Datetime

    :return: Timestamp in epoch
    :rtype: ``int``
    """
    if not d:
        d = datetime.utcnow()
    return int((d - datetime.utcfromtimestamp(0)).total_seconds())


def get_encrypted(content, key):
    """
    Encrypts content with encryption key.

    :type content: ``str``
    :param content: Content to encrypt

    :type key: ``str``
    :param key: encryption key from Oproxy

    :return: Encrypted content
    :rtype: ``timestamp``
    """

    def create_nonce():
        return os.urandom(12)

    def encrypt(string, enc_key):
        """
        Encrypts string input with encryption key.

        :type string: ``str``
        :param string: String to encrypt

        :type enc_key: ``str``
        :param enc_key: Encryption key

        :return: Encrypted value
        :rtype: ``bytes``
        """
        # String to bytes
        enc_key = base64.b64decode(enc_key)
        # Create key
        aes_gcm = AESGCM(enc_key)
        # Create nonce
        nonce = create_nonce()
        # Create ciphered data
        data = string.encode()
        ct = aes_gcm.encrypt(nonce, data, None)
        return base64.b64encode(nonce + ct)

    now = epoch_seconds()
    encrypted = encrypt(f'{now}:{content}', key).decode('utf-8')
    return encrypted


def get_now_utc():
    """
    Creates UTC current time of format Y-m-dTH:M:SZ (e.g. 2019-11-06T09:06:39Z)

    :return: String format of current UTC time
    :rtype: ``str``
    """
    return datetime.utcnow().strftime(DATE_FORMAT)


def add_second_to_str_date(date_string, seconds=1):
    """
    Add seconds to date string.

    Is used as workaround to Graph API bug, for more information go to:
    https://stackoverflow.com/questions/35729273/office-365-graph-api-greater-than-filter-on-received-date

    :type date_string: ``str``
    :param date_string: Date string to add seconds

    :type seconds: int
    :param seconds: Seconds to add to date, by default is set to 1

    :return: Date time string appended seconds
    :rtype: ``str``
    """
    added_result = datetime.strptime(date_string, DATE_FORMAT) + timedelta(seconds=seconds)
    return datetime.strftime(added_result, DATE_FORMAT)


def upload_file(filename, content, attachments_list):
    """
    Uploads file to War room.

    :type filename: ``str``
    :param filename: file name to upload

    :type content: ``str``
    :param content: Content of file to upload

    :type attachments_list: ``list``
    :param attachments_list: List of uploaded file data to War Room
    """
    file_result = fileResult(filename, content)

    if file_result['Type'] == entryTypes['error']:
        demisto.error(file_result['Contents'])
        raise Exception(file_result['Contents'])

    attachments_list.append({
        'path': file_result['FileID'],
        'name': file_result['File'],
    })


def read_file_and_encode64(attach_id):
    """
    Reads file that was uploaded to War Room and encodes it's content to base 64.

    :type attach_id: ``str``
    :param attach_id: The id of uploaded file to War Room

    :return: Base 64 encoded data, size of the encoded data in bytes and uploaded file name.
    :rtype: ``bytes``, ``int``, ``str``
    """
    try:
        file_info = demisto.getFilePath(attach_id)
        with open(file_info['path'], 'rb') as file:
            b64_encoded_data = base64.b64encode(file.read())
            file_size = os.path.getsize(file_info['path'])
            return b64_encoded_data, file_size, file_info['name']
    except Exception as e:
        return_error(f'Unable to read and decode in base 64 file with id {attach_id}', e)


def prepare_args(command):
    """
    Receives command and prepares the arguments for future usage.

    :type command: ``str``
    :param command: Command to execute

    :return: Prepared args
    :rtype: ``dict``
    """
    args = demisto.args()

    if command in ['msgraph-mail-create-draft', 'send-mail']:
        return {
            'to_recipients': argToList(args.get('to')),
            'cc_recipients': argToList(args.get('cc')),
            'bcc_recipients': argToList(args.get('bcc')),
            'subject': args.get('subject', ''),
            'body': args.get('body', ''),
            'body_type': args.get('body_type', 'text'),
            'flag': args.get('flag', 'notFlagged'),
            'importance': args.get('importance', 'Low'),
            'internet_message_headers': argToList(args.get('headers')),
            'attach_ids': argToList(args.get('attach_ids')),
            'attach_names': argToList(args.get('attach_names')),
            'attach_cids': argToList((args.get('attach_cids')))
        }
    elif command == 'msgraph-mail-reply-to':
        return {
            'to_recipients': argToList(args.get('to')),
            'message_id': args.get('message_id', ''),
            'comment': args.get('comment')
        }
    return args


''' MICROSOFT GRAPH MAIL CLIENT '''


class MsGraphClient(BaseClient):
    """
    Microsoft Graph Mail Client enables authorized access to a user's Office 365 mail data in a personal account.
    """
    ITEM_ATTACHMENT = '#microsoft.graph.itemAttachment'
    FILE_ATTACHMENT = '#microsoft.graph.fileAttachment'
    CONTEXT_DRAFT_PATH = 'MicrosoftGraph.Draft(val.ID == obj.ID)'
    CONTEXT_SENT_EMAIL_PATH = 'MicrosoftGraph.Email'

    def __init__(self, refresh_token, auth_id, enc_key, token_retrieval_url, app_name,
                 mailbox_to_fetch, folder_to_fetch, first_fetch_interval, emails_fetch_limit, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._refresh_token = refresh_token
        self._auth_id = auth_id
        self._enc_key = enc_key
        self._token_retrieval_url = token_retrieval_url
        self._app_name = app_name
        self._mailbox_to_fetch = mailbox_to_fetch
        self._folder_to_fetch = folder_to_fetch
        self._first_fetch_interval = first_fetch_interval
        self._emails_fetch_limit = emails_fetch_limit

    def _get_access_token(self):
        """
        Obtains access and refresh token from Oprxoy server. Access token is used and stored in the integration context
        until expiration time. After expiration, new refresh token and access token are obtained and stored in the
        integration context.

        :return: Access token that will be added to authorization header
        :rtype: ``str``
        """
        integration_context = demisto.getIntegrationContext()
        access_token = integration_context.get('access_token')
        valid_until = integration_context.get('valid_until')
        if access_token and valid_until:
            if epoch_seconds() < valid_until:
                return access_token

        oproxy_response = requests.post(
            self._token_retrieval_url,
            json={
                'app_name': self._app_name,
                'registration_id': self._auth_id,
                'encrypted_token': get_encrypted(self._refresh_token, self._enc_key)
            },
            verify=self._verify
        )

        if oproxy_response.status_code not in {200, 201}:
            msg = 'Error in authentication. Try checking the credentials you entered.'
            try:
                demisto.info('Authentication failure from server: {} {} {}'.format(
                    oproxy_response.status_code, oproxy_response.reason, oproxy_response.text))
                err_response = oproxy_response.json()
                server_msg = err_response.get('message')
                if not server_msg:
                    title = err_response.get('title')
                    detail = err_response.get('detail')
                    if title:
                        server_msg = f'{title}. {detail}'
                if server_msg:
                    msg += ' Server message: {}'.format(server_msg)
            except Exception as ex:
                demisto.error('Failed parsing error response - Exception: {}'.format(ex))
            raise Exception(msg)
        try:
            gcloud_function_exec_id = oproxy_response.headers.get('Function-Execution-Id')
            demisto.info(f'Google Cloud Function Execution ID: {gcloud_function_exec_id}')
            parsed_response = oproxy_response.json()
        except ValueError:
            raise Exception(
                'There was a problem in retrieving an updated access token.\n'
                'The response from the Oproxy server did not contain the expected content.'
            )
        access_token = parsed_response.get('access_token')
        expires_in = parsed_response.get('expires_in', 3595)
        current_refresh_token = parsed_response.get('refresh_token')
        time_now = epoch_seconds()
        time_buffer = 5  # seconds by which to shorten the validity period
        if expires_in - time_buffer > 0:
            # err on the side of caution with a slightly shorter access token validity period
            expires_in = expires_in - time_buffer

        demisto.setIntegrationContext({
            'access_token': access_token,
            'valid_until': time_now + expires_in,
            'current_refresh_token': current_refresh_token
        })
        return access_token

    def _http_request(self, *args, **kwargs):
        """
        Overrides Base client request function, retrieves and adds to headers access token before sending the request.
        """
        token = self._get_access_token()
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }
        return super()._http_request(*args, headers=headers, **kwargs)

    def _get_root_folder_children(self, user_id):
        """
        Get the root folder (Top Of Information Store) children collection.

        :type user_id: ``str``
        :param user_id: Mailbox address

        :raises: ``Exception``: No folders found under Top Of Information Store folder

        :return: List of root folder children
        rtype: ``list``
        """
        suffix_endpoint = f'users/{user_id}/mailFolders/msgfolderroot/childFolders?$top=250'
        root_folder_children = self._http_request('GET', suffix_endpoint).get('value', None)
        if not root_folder_children:
            return_error("No folders found under Top Of Information Store folder")

        return root_folder_children

    def _get_folder_children(self, user_id, folder_id):
        """
        Get the folder collection under the specified folder.

        :type user_id ``str``
        :param user_id: Mailbox address

        :type folder_id: ``str``
        :param folder_id: Folder id

        :return: List of folders that contain basic folder information
        :rtype: ``list``
        """
        suffix_endpoint = f'users/{user_id}/mailFolders/{folder_id}/childFolders?$top=250'
        folder_children = self._http_request('GET', suffix_endpoint).get('value', [])
        return folder_children

    def _get_folder_info(self, user_id, folder_id):
        """
        Returns folder information.

        :type user_id: ``str``
        :param user_id: Mailbox address

        :type folder_id: ``str``
        :param folder_id: Folder id

        :raises: ``Exception``: No info found for folder {folder id}

        :return: Folder information if found
        :rtype: ``dict``
        """

        suffix_endpoint = f'users/{user_id}/mailFolders/{folder_id}'
        folder_info = self._http_request('GET', suffix_endpoint)
        if not folder_info:
            return_error(f'No info found for folder {folder_id}')
        return folder_info

    def _get_folder_by_path(self, user_id, folder_path):
        """
        Searches and returns basic folder information.

        Receives mailbox address and folder path (e.g Inbox/Phishing) and iteratively retrieves folders info until
        reaches the last folder of a path. In case that such folder exist, basic information that includes folder id,
        display name, parent folder id, child folders count, unread items count and total items count will be returned.

        :type user_id: ``str``
        :param user_id: Mailbox address

        :type folder_path: ``str``
        :param folder_path: Folder path of searched folder

        :raises: ``Exception``: No such folder exist: {folder path}

        :return: Folder information if found
        :rtype: ``dict``
        """
        folders_names = folder_path.replace('\\', '/').split('/')  # replaced backslash in original folder path

        # Optimization step in order to improve performance before iterating the folder path in order to skip API call
        # for getting Top of Information Store children collection if possible.
        if folders_names[0].lower() in WELL_KNOWN_FOLDERS:
            # check if first folder in the path is known folder in order to skip not necessary api call
            folder_id = WELL_KNOWN_FOLDERS[folders_names[0].lower()]  # get folder shortcut instead of using folder id
            if len(folders_names) == 1:  # in such case the folder path consist only from one well known folder
                return self._get_folder_info(user_id, folder_id)
            else:
                current_directory_level_folders = self._get_folder_children(user_id, folder_id)
                folders_names.pop(0)  # remove the first folder name from the path before iterating
        else:  # in such case the optimization step is skipped
            # current_directory_level_folders will be set to folders that are under Top Of Information Store (root)
            current_directory_level_folders = self._get_root_folder_children(user_id)

        for index, folder_name in enumerate(folders_names):
            # searching for folder in current_directory_level_folders list by display name or id
            found_folder = [f for f in current_directory_level_folders if
                            f.get('displayName', '').lower() == folder_name.lower() or f.get('id', '') == folder_name]

            if not found_folder:  # no folder found, return error
                return_error(f'No such folder exist: {folder_path}')
            found_folder = found_folder[0]  # found_folder will be list with only one element in such case

            if index == len(folders_names) - 1:  # reached the final folder in the path
                # skip get folder children step in such case
                return found_folder
            # didn't reach the end of the loop, set the current_directory_level_folders to folder children
            current_directory_level_folders = self._get_folder_children(user_id, found_folder.get('id', ''))

    def _fetch_last_emails(self, folder_id, last_fetch, exclude_ids):
        """
        Fetches emails from given folder that were received after specific datetime (last_fetch).

        All fields are fetched for given email using select=* clause,
        for more information https://docs.microsoft.com/en-us/graph/query-parameters.
        The email will be excluded from returned results if it's id is presented in exclude_ids.
        Number of fetched emails is limited by _emails_fetch_limit parameter.

        :type folder_id: ``str``
        :param folder_id: Folder id

        :type last_fetch: ``dict``
        :param last_fetch: Previous fetch data

        :type exclude_ids: ``list``
        :param exclude_ids: List of previous fetch email ids to exclude in current run

        :return: Fetched emails and exclude ids list that contains the new ids of fetched emails
        :rtype: ``list`` and ``list``
        """
        target_received_time = add_second_to_str_date(last_fetch)  # workaround to Graph API bug
        suffix_endpoint = (f"users/{self._mailbox_to_fetch}/mailFolders/{folder_id}/messages"
                           f"?$filter=receivedDateTime ge {target_received_time}"
                           f"&$orderby=ReceivedDateTime &$top={self._emails_fetch_limit}&select=*")
        fetched_emails = self._http_request('GET', suffix_endpoint).get('value', [])[:self._emails_fetch_limit]

        if exclude_ids:  # removing emails in order to prevent duplicate incidents
            fetched_emails = [email for email in fetched_emails if email.get('id') not in exclude_ids]

        fetched_emails_ids = [email.get('id') for email in fetched_emails]
        return fetched_emails, fetched_emails_ids

    @staticmethod
    def _get_next_run_time(fetched_emails, start_time):
        """
        Returns receive time of last email if exist, else utc time that was passed as start_time.

        The elements in fetched emails are ordered by received time in ascending order,
        meaning the last element has the latest receive time.

        :type fetched_emails: ``list``
        :param fetched_emails: List of fetched emails

        :type start_time: ``str``
        :param start_time: utc string of format Y-m-dTH:M:SZ

        :return: Returns str date of format Y-m-dTH:M:SZ
        :rtype: `str`
        """
        next_run_time = fetched_emails[-1].get('receivedDateTime') if fetched_emails else start_time

        return next_run_time

    @staticmethod
    def _get_recipient_address(email_address):
        """
        Receives dict of form  "emailAddress":{"name":"_", "address":"_"} and return the address

        :type email_address: ``dict``
        :param email_address: Recipient address

        :return: The address of recipient
        :rtype: ``str``
        """
        return email_address.get('emailAddress', {}).get('address', '')

    @staticmethod
    def _parse_email_as_labels(parsed_email):
        """
        Parses the email as incident labels.

        :type parsed_email: ``dict``
        :param parsed_email: The parsed email from which create incidents labels.

        :return: Incident labels
        :rtype: ``list``
        """
        labels = []

        for (key, value) in parsed_email.items():
            if key == 'Headers':
                headers_labels = [
                    {'type': 'Email/Header/{}'.format(header.get('name', '')), 'value': header.get('value', '')}
                    for header in value]
                labels.extend(headers_labels)
            elif key in ['To', 'Cc', 'Bcc']:
                recipients_labels = [{'type': f'Email/{key}', 'value': recipient} for recipient in value]
                labels.extend(recipients_labels)
            else:
                labels.append({'type': f'Email/{key}', 'value': f'{value}'})

        return labels

    @staticmethod
    def _parse_item_as_dict(email):
        """
        Parses basic data of email.

        Additional info https://docs.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0

        :type email: ``dict``
        :param email: Email to parse

        :return: Parsed email
        :rtype: ``dict``
        """
        parsed_email = {EMAIL_DATA_MAPPING[k]: v for (k, v) in email.items() if k in EMAIL_DATA_MAPPING}
        parsed_email['Headers'] = email.get('internetMessageHeaders', [])

        email_body = email.get('body', {}) or email.get('uniqueBody', {})
        parsed_email['Body'] = email_body.get('content', '')
        parsed_email['BodyType'] = email_body.get('contentType', '')

        parsed_email['Sender'] = MsGraphClient._get_recipient_address(email.get('sender', {}))
        parsed_email['From'] = MsGraphClient._get_recipient_address(email.get('from', {}))
        parsed_email['To'] = list(map(MsGraphClient._get_recipient_address, email.get('toRecipients', [])))
        parsed_email['Cc'] = list(map(MsGraphClient._get_recipient_address, email.get('ccRecipients', [])))
        parsed_email['Bcc'] = list(map(MsGraphClient._get_recipient_address, email.get('bccRecipients', [])))

        return parsed_email

    @staticmethod
    def _build_recipient_input(recipients):
        """
        Builds legal recipients list.

        :type recipients: ``list``
        :param recipients: List of recipients

        :return: List of email addresses recipients
        :rtype: ``list``
        """
        return [{'emailAddress': {'address': r}} for r in recipients] if recipients else []

    @staticmethod
    def _build_body_input(body, body_type):
        """
        Builds message body input.

        :type body: ``str``
        :param body: The body of the message

        :type body_type: The body type of the message, html or text.
        :param body_type:

        :return: The message body
        :rtype ``dict``
        """
        return {
            "content": body,
            "contentType": body_type
        }

    @staticmethod
    def _build_flag_input(flag):
        """
        Builds flag status of the message.

        :type flag: ``str``
        :param flag: The flag of the message

        :return: The flag status of the message
        :rtype ``dict``
        """
        return {'flagStatus': flag}

    @staticmethod
    def _build_headers_input(internet_message_headers):
        """
        Builds valid headers input.

        :type internet_message_headers: ``list``
        :param internet_message_headers: List of headers to build.

        :return: List of transformed headers
        :rtype: ``list``
        """
        return [{'name': kv[0], 'value': kv[1]} for kv in (h.split(':') for h in internet_message_headers)]

    @classmethod
    def _build_attachments_input(cls, ids, attach_names=None, is_inline=False):
        """
        Builds valid attachment input of the message. Is used for both in-line and regular attachments.

        :type ids: ``list``
        :param ids: List of uploaded to War Room files ids

        :type attach_names: ``list``
        :param attach_names: List of attachment name, not required.

        :type is_inline: ``bool``
        :param is_inline: Indicates whether the attachment is inline or not

        :return: List of valid attachments of message
        :rtype: ``list``
        """
        provided_names = bool(attach_names)

        if provided_names and len(ids) != len(attach_names):
            return_error("Invalid input, attach_ids and attach_names lists should be the same length.")

        file_attachments_result = []
        # in case that no attach names where provided, ids are zipped together and the attach_name value is ignored
        attachments = zip(ids, attach_names) if provided_names else zip(ids, ids)

        for attach_id, attach_name in attachments:
            b64_encoded_data, file_size, uploaded_file_name = read_file_and_encode64(attach_id)
            attachment = {
                '@odata.type': cls.FILE_ATTACHMENT,
                'contentBytes': b64_encoded_data.decode('utf-8'),
                'isInline': is_inline,
                'name': attach_name if provided_names else uploaded_file_name,
                'size': file_size
            }
            file_attachments_result.append(attachment)

        return file_attachments_result

    @staticmethod
    def _build_file_attachments_input(attach_ids, attach_names, attach_cids):
        """
        Builds both inline and regular attachments.

        :type attach_ids: ``list``
        :param attach_ids: List of uploaded to War Room regular attachments to send

        :type attach_names: ``list``
        :param attach_names: List of regular attachments names to send

        :type attach_cids: ``list``
        :param attach_cids: List of uploaded to War Room inline attachments to send

        :return: List of both inline and regular attachments of the message
        :rtype: ``list``
        """
        regular_attachments = MsGraphClient._build_attachments_input(ids=attach_ids, attach_names=attach_names)
        inline_attachments = MsGraphClient._build_attachments_input(ids=attach_cids, is_inline=True)

        return regular_attachments + inline_attachments

    @staticmethod
    def _build_message(to_recipients, cc_recipients, bcc_recipients, subject, body, body_type, flag, importance,
                       internet_message_headers, attach_ids, attach_names, attach_cids):
        """
        Builds valid message dict.
        For more information https://docs.microsoft.com/en-us/graph/api/resources/message?view=graph-rest-1.0
        """
        message = {
            'toRecipients': MsGraphClient._build_recipient_input(to_recipients),
            'ccRecipients': MsGraphClient._build_recipient_input(cc_recipients),
            'bccRecipients': MsGraphClient._build_recipient_input(bcc_recipients),
            'subject': subject,
            'body': MsGraphClient._build_body_input(body=body, body_type=body_type),
            'bodyPreview': body[:255],
            'importance': importance,
            'flag': MsGraphClient._build_flag_input(flag),
            'attachments': MsGraphClient._build_file_attachments_input(attach_ids, attach_names, attach_cids)
        }

        if internet_message_headers:
            message['internetMessageHeaders'] = MsGraphClient._build_headers_input(internet_message_headers)

        return message

    @staticmethod
    def _build_reply(to_recipients, comment):
        """
        Builds the reply message that includes recipients to reply and reply message.

        :type to_recipients: ``list``
        :param to_recipients: The recipients list to reply

        :type comment: ``str``
        :param comment: The message to reply.

        :return: Returns legal reply message.
        :rtype: ``dict``
        """
        return {
            'message': {
                'toRecipients': MsGraphClient._build_recipient_input(to_recipients)
            },
            'comment': comment
        }

    def _get_attachment_mime(self, attachment_id):
        """
        Gets attachment mime.

        :type attachment_id: ``str``
        :param attachment_id: Attachment id to get MIME

        :return: The MIME of the attachment
        :rtype: ``str``
        """
        suffix_endpoint = f'users/{self._mailbox_to_fetch}/messages/{attachment_id}/$value'
        mime_content = self._http_request('GET', suffix_endpoint, resp_type='text')

        return mime_content

    def _get_email_attachments(self, message_id):
        """
        Get email attachments  and upload to War Room.

        :type message_id: ``str``
        :param message_id: The email id to get attachments

        :return: List of uploaded to War Room data, uploaded file path and name
        :rtype: ``list``
        """

        attachment_results = []  # type: ignore
        suffix_endpoint = f'users/{self._mailbox_to_fetch}/messages/{message_id}/attachments'
        attachments = self._http_request('Get', suffix_endpoint).get('value', [])

        for attachment in attachments:
            attachment_type = attachment.get('@odata.type', '')
            attachment_name = attachment.get('name', 'untitled_attachment')
            if attachment_type == self.FILE_ATTACHMENT:
                try:
                    attachment_content = base64.b64decode(attachment.get('contentBytes', ''))
                except Exception as e:  # skip the uploading file step
                    demisto.info(f"MS-Graph-Listener: failed in decoding base64 file attachment with error {str(e)}")
                    continue
            elif attachment_type == self.ITEM_ATTACHMENT:
                attachment_id = attachment.get('id', '')
                attachment_content = self._get_attachment_mime(attachment_id)
                attachment_name = f'{attachment_name}.eml'
            # upload the item/file attachent to War Room
            upload_file(attachment_name, attachment_content, attachment_results)

        return attachment_results

    def _parse_email_as_incident(self, email):
        """
        Parses fetched emails as incidents.

        :type email: ``dict``
        :param email: Fetched email to parse

        :return: Parsed email
        :rtype: ``dict``
        """
        parsed_email = MsGraphClient._parse_item_as_dict(email)

        if email.get('hasAttachments', False):  # handling attachments of fetched email
            parsed_email['Attachments'] = self._get_email_attachments(message_id=email.get('id', ''))

        incident = {
            'name': parsed_email['Subject'],
            'details': email.get('bodyPreview', '') or parsed_email['Body'],
            'labels': MsGraphClient._parse_email_as_labels(parsed_email),
            'occurred': parsed_email['ReceivedTime'],
            'attachment': parsed_email.get('Attachments', []),
            'rawJSON': json.dumps(parsed_email)
        }

        return incident

    @logger
    def fetch_incidents(self, last_run):
        """
        Fetches emails from office 365 mailbox and creates incidents of parsed emails.

        :type last_run: ``dict``
        :param last_run:
            Previous fetch run data that holds the fetch time in utc Y-m-dTH:M:SZ format,
            ids of fetched emails, id and path of folder to fetch incidents from

        :return: Next run data and parsed fetched incidents
        :rtype: ``dict`` and ``list``
        """
        start_time = get_now_utc()
        last_fetch = last_run.get('LAST_RUN_TIME', None)
        exclude_ids = last_run.get('LAST_RUN_IDS', [])
        last_run_folder_path = last_run.get('LAST_RUN_FOLDER_PATH')
        folder_path_changed = False

        if last_run_folder_path != self._folder_to_fetch:
            # detected folder path change, get new folder id and set flag to true
            folder_id = self._get_folder_by_path(self._mailbox_to_fetch, self._folder_to_fetch).get('id')
            folder_path_changed = True
            demisto.info("MS-Graph-Listener: detected file path change, ignored last run.")
        else:
            # LAST_RUN_FOLDER_ID is stored in order to avoid calling _get_folder_by_path method in each fetch
            folder_id = last_run.get('LAST_RUN_FOLDER_ID')

        if not last_fetch or folder_path_changed:  # initialized fetch
            last_fetch, _ = parse_date_range(self._first_fetch_interval, date_format=DATE_FORMAT, utc=True)
            demisto.info(f"MS-Graph-Listener: initialize fetch and pull emails from date :{last_fetch}")

        fetched_emails, fetched_emails_ids = self._fetch_last_emails(folder_id=folder_id, last_fetch=last_fetch,
                                                                     exclude_ids=exclude_ids)
        incidents = list(map(self._parse_email_as_incident, fetched_emails))
        next_run_time = MsGraphClient._get_next_run_time(fetched_emails, start_time)
        next_run = {
            'LAST_RUN_TIME': next_run_time,
            'LAST_RUN_IDS': fetched_emails_ids,
            'LAST_RUN_FOLDER_ID': folder_id,
            'LAST_RUN_FOLDER_PATH': self._folder_to_fetch
        }
        demisto.info(f"MS-Graph-Listener: fetched {len(incidents)} incidents")

        return next_run, incidents

    def create_draft(self, **kwargs):
        """
        Creates draft message in user's mailbox, in draft folder.
        """
        suffix_endpoint = f'/users/{self._mailbox_to_fetch}/messages'
        draft = MsGraphClient._build_message(**kwargs)

        created_draft = self._http_request('POST', suffix_endpoint, json_data=draft)
        parsed_draft = MsGraphClient._parse_item_as_dict(created_draft)
        human_readable = tableToMarkdown(f'Created draft with id: {parsed_draft.get("ID", "")}', parsed_draft)
        ec = {self.CONTEXT_DRAFT_PATH: parsed_draft}

        return human_readable, ec, created_draft

    def send_email(self, **kwargs):
        """
        Sends email from user's mailbox, the sent message will appear in Sent Items folder
        """
        suffix_endpoint = f'/users/{self._mailbox_to_fetch}/sendMail'
        message_content = MsGraphClient._build_message(**kwargs)
        self._http_request('POST', suffix_endpoint, json_data={'message': message_content},
                           resp_type="text")

        message_content.pop('attachments', None)
        message_content.pop('internet_message_headers', None)
        human_readable = tableToMarkdown(f'Email was sent successfully.', message_content)
        ec = {self.CONTEXT_SENT_EMAIL_PATH: message_content}

        return human_readable, ec

    def reply_to(self, to_recipients, comment, message_id):
        """
        Sends reply message to recipients.

        :type to_recipients: ``list``
        :param to_recipients: List of recipients to reply.

        :type comment: ``str``
        :param comment: The comment to send as a reply

        :type message_id: ``str``
        :param message_id: The message id to reply.

        :return: String representation of markdown message regarding successful message submission.
        rtype: ``str``
        """
        suffix_endpoint = f'/users/{self._mailbox_to_fetch}/messages/{message_id}/reply'
        reply = MsGraphClient._build_reply(to_recipients, comment)
        self._http_request('POST', suffix_endpoint, json_data=reply, resp_type="text")

        return f'### Replied to: {", ".join(to_recipients)} with comment: {comment}'

    def send_draft(self, draft_id):
        """
        Send draft message.

        :type draft_id: ``str``
        :param draft_id: Draft id to send.

        :return: String representation of markdown message regarding successful message submission.
        :rtype: ``str``
        """
        suffix_endpoint = f'/users/{self._mailbox_to_fetch}/messages/{draft_id}/send'
        self._http_request('POST', suffix_endpoint, resp_type="text")

        return f'### Draft with: {draft_id} id was sent successfully.'

    def test_connection(self):
        """
        Basic connection test instead of test-module.

        :return: Returns markdown string representation of success or Exception in case of login failure.
        rtype: ``str`` or Exception
        """
        suffix_endpoint = f'users/{self._mailbox_to_fetch}'
        user_response = self._http_request('GET', suffix_endpoint)

        if user_response.get('mail') != '' and user_response.get('id') != '':
            return_outputs('```✅ Success!```')
        else:
            raise Exception("Failed validating the user.")


def main():
    """ COMMANDS MANAGER / SWITCH PANEL """
    params = demisto.params()
    # params related to oproxy
    auth_id = params.get('auth_id')
    # In case the script is running for the first time, refresh token is retrieved from integration parameters,
    # in other case it's retrieved from integration context.
    refresh_token = demisto.getIntegrationContext().get('current_refresh_token') or params.get('refresh_token', '')
    enc_key = params.get('enc_key')
    token_retrieval_url = 'https://oproxy.demisto.ninja/obtain-token'  # disable-secrets-detection
    app_name = 'ms-graph-mail-listener'

    # params related to common instance configuration
    base_url = 'https://graph.microsoft.com/v1.0/'
    verify = not params.get('insecure', False)
    proxy = params.get('proxy', False)

    # params related to mailbox to fetch incidents
    mailbox_to_fetch = params.get('mailbox_to_fetch', '')
    folder_to_fetch = params.get('folder_to_fetch', 'Inbox')
    first_fetch_interval = params.get('first_fetch_interval', '15 minutes')
    emails_fetch_limit = int(params.get('emails_fetch_limit', '50'))
    ok_codes = (200, 201, 202)

    client = MsGraphClient(refresh_token, auth_id, enc_key, token_retrieval_url, app_name, mailbox_to_fetch,
                           folder_to_fetch, first_fetch_interval, emails_fetch_limit, base_url=base_url, verify=verify,
                           proxy=proxy, ok_codes=ok_codes)

    try:
        command = demisto.command()
        args = prepare_args(command)
        LOG(f'Command being called is {command}')

        if command == 'test-module':
            # cannot use test module due to the lack of ability to set refresh token to integration context
            raise Exception("Please use !msgraph-mail-test instead")
        if command == 'msgraph-mail-test':
            client.test_connection()
        if command == 'fetch-incidents':
            next_run, incidents = client.fetch_incidents(demisto.getLastRun())
            demisto.setLastRun(next_run)
            demisto.incidents(incidents)
        elif command == 'msgraph-mail-create-draft':
            human_readable, ec, raw_response = client.create_draft(**args)
            return_outputs(human_readable, ec, raw_response)
        elif command == 'msgraph-mail-reply-to':
            human_readable = client.reply_to(**args)
            return_outputs(human_readable)
        elif command == 'msgraph-mail-send-draft':
            human_readable = client.send_draft(**args)
            return_outputs(human_readable)
        elif command == 'send-mail':
            human_readable, ec = client.send_email(**args)
            return_outputs(human_readable, ec)
    except Exception as e:
        return_error(str(e))


if __name__ in ['__main__', '__builtin__', 'builtins']:
    main()
