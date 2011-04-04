'''
Created on Apr 1, 2011

@author: dinkydogg
'''

import os
import re
import json
import io
import base64
import tornado.web
import tornado.auth
import bcrypt
import Image
import urllib2
import tornadorpc.json
import functools
import model
import base36


class MustOwnPlaylistException(Exception): pass
class PlaylistNotFoundException(Exception): pass


def canonicalize(string):
    string = re.sub('[^a-zA-Z0-9]+', ' ', string)
    ' '.join([word.capitalize() for word in string.split()])
    string = re.sub(' ', '-', string)
    return string


def ownsPlaylist(method):
    """ Decorator: throws an exception if user doesn't own current playlist
    
    NOTE: playlist_id must be the 1st positional arg. If you put some other
    value as the 1st positional arg it could be a security issue (as this 
    would check that the user owns the wrong playlist ID.) So make sure 
    playlist_id is the first positional arg.
    
    I need to read more about python to figure out if there's a better way to
    do this.
    """ 
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        playlist_id = (kwargs['playlist_id'] 
                       if 'playlist_id' in kwargs 
                       else args[0])
        playlist = self.db_session.query(model.Playlist).get(playlist_id)
        if not self.owns_playlist(playlist):
            raise MustOwnPlaylistException()
        return method(self, *args, **kwargs)
    return wrapper

        
class HandlerBase(tornado.web.RequestHandler):
    
    """ All handlers should extend this """
    
    db_session = model.DbSession()
    
    # Cache the session and user
    current_session = None
    current_user = None
  
    def get_error_html(self, status_code, **kwargs):
        """Renders error pages (called internally by Tornado)"""
        if status_code == 404:
            return open('static/404.html', 'r').read()
                
        return super(HandlerBase, self).get_error_html(status_code, **kwargs)
          
    def get_current_user(self):
        if self.current_user is not None:
            return self.current_user
        
        self.current_user = (self.db_session.query(model.Session)
                                .get(self.get_current_session().id)
                                .user)
        
        return self.current_user
    
    def get_current_session(self):
        if self.current_session is not None:
            return self.current_session
        
        session_id = self.get_secure_cookie('session_id')
        if session_id is not None:
            self.current_session = (self.db_session.query(model.Session)
                                       .get(int(session_id)))
            
        if self.current_session is None:
            self.current_session = model.Session()
            self.db_session.add(self.current_session)
            self.db_session.commit()
            self.set_secure_cookie('session_id', str(self.current_session.id))
            
        return self.current_session
        
    def get_profile_url(self):
        user = self.get_current_user()
        return '/user/' + user.profile if user is not None else ''
 
    def owns_playlist(self, playlist):
        if playlist is None:
            return False
        
        session = self.get_current_session()
        user = self.get_current_user()
        
        return ((session.id is not None and str(session.id) == playlist.session_id) 
                or (user is not None and user.id == playlist.user_id))
          
    def _log_user_in(self, user, expire_on_browser_close=False):
        # Promote playlists, uploaded images, and session to be owned by user
        session = self.get_current_session()
        
        (self.db_session.query(model.Playlist)
            .filter_by(session_id=session.id)
            .update({"user_id": user.id}))
        
        (self.db_session.query(model.Image)
            .filter_by(session_id=session.id)
            .update({"user_id": user.id}))
        
        session.user_id = user.id
        self.db_session.commit()
       
        # Set cookies for user_id and user_name
        # TODO: Make this use javascript variables instead of cookies.
        expires_days = 30 if not expire_on_browser_close else None
        self.set_cookie('user_id', str(user.id), expires_days=expires_days)
        self.set_cookie('user_name', urllib2.quote(user.name), expires_days=expires_days)
        self.set_cookie('profile_url', urllib2.quote(self.get_profile_url()), expires_days=expires_days)
        
    def _log_user_out(self):
        session_id = self.get_secure_cookie('session_id')
        if session_id:
            self.db_session.query(model.Session).filter_by(id=session_id).delete()
            
        self.clear_cookie('session_id')
        self.clear_cookie('session_num')
        self.clear_cookie('user_id')
        self.clear_cookie('user_name')
        self.clear_cookie('profile')

            
class PlaylistHandlerBase(HandlerBase):
    """ Any handler that involves playlists should extend this.
    """ 
    
    def _sanitize_songlist_json(self, json_str):
        uploaded_list = json.loads(json_str)
        songlist = []
        
        url_re = re.compile('^(http://userserve-ak\.last\.fm/|http://images.amazon.com/images/)') 
        
        for song in uploaded_list:
            title = song['t'] if song.has_key('t') else None
            artist = song['a'] if song.has_key('a') else None
            image = song['i'] if song.has_key('i') else None
            
            if title.__class__ == unicode and artist.__class__ == unicode:
                new_song = {'a': artist, 't': title}
                if image.__class__ == unicode and url_re.match(image) is not None:
                    new_song['i'] = image
                else:
                    new_song['i'] = None
                songlist.append(new_song)
        
        return json.dumps(songlist)

    def _render_playlist_view(self, template_name, playlist=None, **kwargs):
        template = ('partial/' if self._is_partial() else '') + template_name
        self.render(template, is_partial=self._is_partial(), playlist=playlist, **kwargs)
        
    def _is_partial(self):
        return self.get_argument('partial', default=False)
        
    def render_user_name(self):
        user = self.get_current_user()
        name = user.name if user else ''
        return '<span class="username">' + name + '</span>'
       
       
class UploadHandlerBase(HandlerBase):
    def _get_request_content(self):
        # If the file is directly uploaded in the POST body
        # Make a dict of the headers with all lowercase keys
        lower_headers = dict([(key.lower(), value) for (key, value) in self.request.headers.items()])
        if 'up-filename' in lower_headers:
            filename = lower_headers['up-filename']

            if self.get_argument('base64', 'false') == 'true':
                try:                    
                    contents = base64.b64decode(self.request.body)
                except:
                    return {'status': 'Invalid request'}
            else:
                contents = self.request.body
        # If the file is in form/multipart data
        else:
            if 'file' not in self.request.files or len(self.request.files['file']) == 0:
                return {'status': 'No file specified'}
        
            uploaded_file = self.request.files['file'][0]
            filename = uploaded_file['filename']
            contents = uploaded_file['body']
            
        return (filename, contents)
        

class UserHandlerBase(HandlerBase):
    def _verify_password(self, password, hashed):
        return bcrypt.hashpw(password, hashed) == hashed
        
    def _hash_password(self, password):
        return bcrypt.hashpw(password, bcrypt.gensalt())
    
    def _validate_args(self, args, errors):
        for name, types in args.iteritems():
            value = self.get_argument(name, '', True)
            email_regex = re.compile('^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,4}$')
            if "email" in types and None == email_regex.match(value):
                errors[name] = 'Please enter a valid email.' 
            if "password" in types and len(value) < 4:
                errors[name] = 'Passwords must be at least 4 characters.' 
            if "required" in types and value == '':
                errors[name] = 'The field "' + name + '" is required.' 
            
    def _send_errors(self, errors):
        """ Send errors if there are any. """
        if not errors:
            return False
        
        result = {'errors': errors, 'success': False}
        self.write(json.dumps(result))
        self.finish()
        return True
    
    def _is_registered_fbid(self, fb_id):
        return self.db_session.query(model.User).filter_by(fb_id=fb_id).count() > 0
         
         
class ImageHandlerBase(HandlerBase):
    STATIC_DIR = 'static'
    IMAGE_DIR = '/images/uploaded/'
    
    def _crop_to_square(self, image):
        cropped_side_length = min(image.size)
        square = ((image.size[0] - cropped_side_length) / 2,
                  (image.size[1] - cropped_side_length) / 2,
                  (image.size[0] + cropped_side_length) / 2,
                  (image.size[1] + cropped_side_length) / 2)
        return image.crop(square)
    
    def _resize(self, image, side_length):
        image = image.copy()
        size = (side_length, side_length)
        image.thumbnail(size, Image.ANTIALIAS)
        return image
    
    def _save_image(self, image_id, image_format, image):
        filename = '{0:x}-{1}x{2}.{3}'.format(image_id, image.size[0], image.size[1], image_format.lower())
        path = os.path.join(self.IMAGE_DIR, filename)
        image.save(self.STATIC_DIR + path, img_format=image.format)
        return path
    
    def _is_valid_image(self, data):
        try:
            image = Image.open(data)
            image.verify()
        except:
            return False
        
        data.seek(0)
        return True
      
    def _handle_image(self, data, playlist_id):
        result = {'status': 'OK', 'images': {}}
        
        if self._is_valid_image(data) == False:
            result['status'] = 'No valid image at that URL.'
            return result
       
        image = model.Image()
        image.user = self.get_current_user()
        image.session = self.get_current_session()
        print(image.session.id)
        self.db_session.add(image)
        playlist = self.db_session.query(model.Playlist).get(playlist_id)
        playlist.image = image
        self.db_session.commit()
        
        original_image = Image.open(data)
        cropped_image = self._crop_to_square(original_image)
        
        image.original = self._save_image(image.id, original_image.format, original_image)
        image.medium = self._save_image(image.id, original_image.format, self._resize(cropped_image, 160))
        self.db_session.commit()
   
    
class JsonRpcHandler(tornadorpc.json.JSONRPCHandler, PlaylistHandlerBase,
                     UserHandlerBase, ImageHandlerBase):
   
    @ownsPlaylist
    def update_songlist(self, playlist_id, songs):
        self.db_session.query(model.Playlist).get(playlist_id).songs = songs
        self.db_session.commit()
        
    @ownsPlaylist
    def update_title(self, playlist_id, title):
        self.db_session.query(model.Playlist).get(playlist_id).title = title
        self.db_session.commit()
        
    @ownsPlaylist
    def update_description(self, playlist_id, description):
        self.db_session.query(model.Playlist).get(playlist_id).description = description
        self.db_session.commit()
        
    def is_registered_fbid(self, fb_id):
        """ Wraps the inherited function so it responds to RPC """
        return self._is_registered_fbid(fb_id)
    
    @tornadorpc.async
    @ownsPlaylist
    def set_image_from_url(self, playlist_id, url):
        self.playlist_id = playlist_id
        http = tornado.httpclient.AsyncHTTPClient()
        http.fetch(url, callback=self._on_set_image_from_url_response)
       
    def _on_set_image_from_url_response(self, response):
        result = self._handle_image(response.buffer, self.playlist_id)
        self.result(result)
 

class GetImagesHandler(HandlerBase):
    def get(self):
        user = self.get_current_user()
        query = self.db_session.query(model.Image)
        query.filter_by(session_id=self.get_current_session())
        if user is not None:
            query.filter_by(user_id=self.get_current_user().id)
        
        return [image.to_json() for image in query.all()]
            
    
class HomeHandler(HandlerBase):
    def get(self):
        self.render("index.html")
        
        
class TermsHandler(HandlerBase):
    def get(self):
        self.render("terms.html")
        
      
class PlaylistHandler(PlaylistHandlerBase):
    """Landing page for a playlist"""
    def get(self, playlist_alpha_id):
        playlist_id = base36.base36_10(playlist_alpha_id)
        playlist = self.db_session.query(model.Playlist).get(playlist_id)
        
        if playlist is None:
            self.send_error(404)
            return
        
        if self.get_argument('json', default=False):
            self.write(playlist.to_json())
        else:
            self.render('playlist.html', playlist=playlist)
            
   
class SearchHandler(PlaylistHandlerBase):
    def get(self):
        self._render_playlist_view('search.html')
        

class ArtistHandler(PlaylistHandlerBase):
    """ Renders an empty artist template """
    def get(self, requested_artist_name):
        self._render_playlist_view('artist.html', artist=None)
            

class AlbumHandler(PlaylistHandlerBase):
    def get(self, requested_artist_name, requested_album_name):
        """ Renders an empty album template """
        self._render_playlist_view('album.html', album=None)
       
      
class UploadHandler(UploadHandlerBase, PlaylistHandlerBase):
    
    """ Handles playlist upload requests """
    
    def _parseM3U(self, contents):
        f = io.StringIO(contents.decode('utf-8'), newline=None)
        
        first_line = f.readline()
        if not re.match(r"#EXTM3U", first_line):
            return None
        
        # Attempt to guess if the artist/title are in iTunes order
        itunes_format = False
        while True:
            line = f.readline()
            if len(line) == 0:
                break
                
            if re.match(r"[^#].*([/\\])iTunes\1", line):
                itunes_format = True
                break
        
        f.seek(0)
        
        res_arr = []        
        while True:
            line = f.readline()
            if len(line) == 0:
                break
            
            line = line.rstrip("\n")

            if itunes_format:
                res = re.match(r"#EXTINF:\d*,(.*) - (.*)", line)
                if res:
                    title = res.group(1)
                    artist = res.group(2)
                    res_arr.append({'t': title, 'a': artist})
                    
            else:
                # Slightly different regex to handle dashes in song titles better
                res = re.match(r"#EXTINF:\d*,(.*?) - (.*)", line)
                if res:
                    artist = res.group(1)
                    title = res.group(2)
                    res_arr.append({'t': title, 'a': artist})
                
        return res_arr
        
    def _parse_text(self, contents):
        try:
            decoded = contents.decode('utf-8')
        except:
            decoded = contents.decode('utf-16')
        
        f = io.StringIO(decoded, newline=None)
        
        first_line = f.readline()
        if not re.match(r"Name\tArtist", first_line):
            return None
            
        res_arr = []
        while True:
            line = f.readline()
            if len(line) == 0:
                break
                
            line = line.rstrip("\n")
            
            res = re.match(r"([^\t]*)\t([^\t]*)", line)
            if res:
                title = res.group(1)
                artist = res.group(2)
                res_arr.append({'t': title, 'a': artist})        
    
        return res_arr

    def _parse_pls(self, contents):
        f = io.StringIO(contents.decode('utf-8'), newline=None)
        
        first_line = f.readline()
        if not re.match(r"\[playlist\]", first_line):
            return None
            
        res_arr = []
        while True:
            line = f.readline()
            if len(line) == 0:
                break
                
            line = line.rstrip("\n")
            
            res = re.match(r"Title\d=(.*?) - (.*)", line)
            if res:
                artist = res.group(1)
                title = res.group(2)
                res_arr.append({'t': title, 'a': artist})
                
        return res_arr
       
    def _handle_request(self, filename, contents):
        title, ext = os.path.splitext(filename)
        
        # Parse the file based on the format
        if ext == ".m3u" or ext == ".m3u8":
            songs = self._parseM3U(contents)
            
        elif ext == ".txt":
            songs = self._parse_text(contents)
            
        elif ext == ".pls":
            songs = self._parse_pls(contents)

        else:
            return {'status': 'Unsupported type'}
        
        # Just in case, we sanitize the playlist's json.
        songs = json.loads(self._sanitize_songlist_json(json.dumps(songs)))
            
        playlist = model.Playlist(title)
        playlist.songs = songs
        self.db_session.add(playlist)
        self.db_session.commit()
        return playlist
    
    def post(self):
        self._get_session_cookie()
        (filename, contents) = self._get_request_content()
        result = self._handle_request(filename, contents)
        
        if self.get_argument('redirect', 'false') == 'true':
            playlist_id = result['playlist_id']
            self.redirect("/p/" + playlist_id)
        else:
            self.set_header("Content-Type", "application/json")
            self.write(json.dumps(result))


class FbSignupHandler(UserHandlerBase,
                      tornado.auth.FacebookGraphMixin):
    @tornado.web.asynchronous
    def post(self):
        # TODO: Find a proper method of validation.
        errors = {}
        args = {'name': ['required'],
                'email': ['required', 'email'],
                'password': ['required', 'password'],
                'fb_user_id': ['required'],
                'auth_token': ['required'],
        }
        
        self._validate_args(args, errors)
           
        # Make sure that FBID and email aren't already taken
        fb_id = self.get_argument('fb_user_id', True)
        email = self.get_argument('email', True)
        if self.db_session.query(model.User).filter_by(fb_id=fb_id).count() > 0:
            errors['fb_user_id'] = 'This Facebook user is already registered on Instant.fm. Try logging in instead.'
        if self.db_session.query(model.User).filter_by(email=email).count() > 0:
            errors['email'] = 'This email is already registered on Instant.fm. Try logging in instead.'
            
        if len(errors.keys()) > 0:
            self._send_errors(errors)
            return
           
        # Authenticate to Facebook
        self.facebook_request(
            "/me",
            access_token=self.get_argument("auth_token"),
            callback=self.async_callback(self._on_auth))
        
    def _on_auth(self, user):
        errors = []
        if user['id'] == self.get_argument('fb_user_id'):
            hashed_pass = self._hash_password(self.get_argument('password'))
            
            # Find an unused prefix name to use
            name = self.get_argument('name') 
            profile = prefix = canonicalize(name)
            collisions = [user.profile for user in 
                            (self.db_session.query(model.User)
                             .filter(model.User.profile.startswith(prefix))
                             .all())]
            suffix = 0
            while profile in collisions:
                suffix += 1
                profile = prefix + '-' + str(suffix)
            
            # Write the user to DB
            user = model.User()
            user.fb_id = self.get_argument('fb_user_id')
            user.name = name
            user.email = self.get_argument('email')
            user.password = hashed_pass
            user.profile = profile
            self.db_session.add(user)
            self.db_session.commit()
            
            self._log_user_in(user)
            self.write(json.dumps(True))
            self.finish()
        else:
            errors['fb_user_id'] = 'Failed to authenticate to Facebook.'
            self._send_errors(errors)
            
        
class LoginHandler(UserHandlerBase):
    def post(self):
        errors = {}
        args = {
            "email": ["required", "email"],
            "password": ["required"],
        }
        self._validate_args(args, errors)
        if self._send_errors(errors):
            return
        
        user = self.db_session.query(model.User).filter_by(email=self.get_argument('email', True)).first()
        if not user:
            errors['email'] = 'No account matching that e-mail address found.'
            if self._send_errors(errors):
                return
            
        if not self._verify_password(self.get_argument('password'), user.password):
            errors['password'] = 'Incorrect password.'
            if self._send_errors(errors):
                return
            
        # If we haven't failed out yet, the login is valid.
        expire_on_browser_close = not self.get_argument('passwordless', False)
        self._log_user_in(user, expire_on_browser_close=expire_on_browser_close)
        self.write(json.dumps(True))
        
        
class NewPlaylistHandler(PlaylistHandlerBase):
    def post(self):
        title = self.get_argument('title', strip=True)
        description = self.get_argument('description', default=None, strip=True)
        
        # Error out if name is empty. Our client-side validation should prevent this
        # from happening, so we don't need an error message.
        if title == '':
            self.send_error(500)
            return
        
        playlist = model.Playlist(title)
        playlist.description = description
        self.db_session.add(playlist)
        self.db_session.commit()
        self.write(playlist.to_json())
        
        
class LogoutHandler(UserHandlerBase):
    def post(self):
        self._log_user_out()
        
        
class ErrorHandler(HandlerBase):
    def prepare(self):
        self.send_error(404)    