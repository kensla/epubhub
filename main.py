import json, logging, urllib, os, re, zipfile
from google.appengine.api import search, taskqueue, users
from google.appengine.ext import blobstore, db, webapp
from google.appengine.ext.webapp import blobstore_handlers, template
from gaesessions import get_current_session
import model, unpack

def enforce_login(handler):
    session = get_current_session()
    account = session.get("account")
    if account is None:
        handler.redirect("/")

def respondWithMessage(handler, message):
    template_values = {
        "current_user" : get_current_session().get("account"),
        "message" : message
    }
    path = os.path.join(os.path.dirname(__file__), 'html/message.html')
    handler.response.out.write(template.render(path, template_values))

class About(webapp.RequestHandler):
    def get(self):
        template_values = {
            "current_user" : get_current_session().get("account"),
        }
        path = os.path.join(os.path.dirname(__file__), 'html/about.html')
        self.response.out.write(template.render(path, template_values))

class Main(webapp.RequestHandler):
    def get(self):
        session = get_current_session()
        user = users.get_current_user()
        if user:
            account = db.GqlQuery("SELECT * FROM Account WHERE googleUserID = :1", user.user_id()).get()
            if account is None:
                account_key = session.get("account")
                account = None if account_key is None else db.get(account_key)
            if account is None:
                account = model.Account(googleUserID = user.user_id(), googleEmail = user.email())
                account.put()
            elif account.googleUserID is None:
                account.googleUserID = user.user_id()
                account.put()
            session["account"] = account.key()

        account_key = session.get("account")
        account = None if account_key is None else db.get(account_key)
        if account is None:
            template_values = {
                "current_user" : get_current_session().get("account"),
                "login_url" : users.create_login_url("/")
            }
            path = os.path.join(os.path.dirname(__file__), 'html/index.html')
            self.response.out.write(template.render(path, template_values))
        else:
            self.redirect('/list')

class LogOut(webapp.RequestHandler):
    def get(self):
        session = get_current_session()
        session.terminate()
        if users.get_current_user():
            self.redirect(users.create_logout_url("/"))
        else:
            self.redirect("/")

class UploadForm(webapp.RequestHandler):
    def get(self):
        enforce_login(self)
        template_values = {
            "current_user" : get_current_session().get("account"),
            "upload_url" : blobstore.create_upload_url('/upload_complete')
        }
        path = os.path.join(os.path.dirname(__file__), 'html/upload.html')
        self.response.out.write(template.render(path, template_values))

class UploadHandler(blobstore_handlers.BlobstoreUploadHandler):
    def post(self):
        enforce_login(self)
        upload_files = self.get_uploads('file')  # 'file' is file upload field in the form
        blob_info = upload_files[0]

        epub = model.ePubFile(blob = blob_info, blob_key = blob_info.key())
        epub.put()
        entry = model.LibraryEntry(epub = epub, user = get_current_session().get("account"))
        entry.put()
        
        unpacker = unpack.Unpacker()
        existing, error = unpacker.unpack(epub)

        if error is None:
            epub_key = epub.key() if existing is None else existing.key()
            logging.info("Indexing epub with key %s" % epub_key)
            taskqueue.add(queue_name = 'index', url='/index', countdown=2, params={
                'key':epub_key,
                'user':get_current_session().get("account")
            })
            epub.get_cover()
            self.redirect("/book/"+str(epub_key.id()))
        else:
            db.delete(entry)
            blobstore.delete(epub.blob.key())
            db.delete(epub)
            error = "Invalid EPUB file" if error.find("File is not a zip")>0 else error
            respondWithMessage(self, "Upload error: "+error)

class UnpackInternal(webapp.RequestHandler):
    def get(self):
        key = self.request.get('key');
        epub = db.get(key)
        unpacker = unpack.Unpacker()
        unpacker.unpack_internal(epub)

class Index(webapp.RequestHandler):
    def get(self):
        return self.post()

    def post(self):
        user = get_current_session().get("account")
        user = self.request.get('user') if user is None else str(user)
        key = self.request.get('key')
        epub = db.get(key)
        if epub is None:
            logging.info("Unable to find epub with key %s" % key)
            return
        unpacker = unpack.Unpacker()
        unpacker.index_epub(epub, user, "private")
        if epub.license == "Public Domain" or epub.license == "Creative Commons":
            unpacker.index_epub(epub, user, "public")
        
class List(webapp.RequestHandler):
    def get(self):
        account = get_current_session().get("account")
        if account is None or self.request.get('show')=="public":
            epubs = model.ePubFile.all().filter("license IN",["Public Domain","Creative Commons"])
        else:
            epubs = []
            entries = model.LibraryEntry.all().filter("user =",db.get(account))
            for entry in entries:
                epubs.append(entry.epub)

        results = []
        idx = 0
        for epub in sorted(epubs, key = lambda epub:epub.title):
            results.append({ 'epub' : epub, 'fourth' : idx%4==0 })
            idx+=1
        template_values = {
            "current_user" : get_current_session().get("account"),
            "upload_url" : blobstore.create_upload_url('/upload_complete'),
            "results" : None if len(results)==0 else results
        }
        path = os.path.join(os.path.dirname(__file__), 'html/books.html')
        self.response.out.write(template.render(path, template_values))

class Manifest(blobstore_handlers.BlobstoreDownloadHandler):
    def get(self):
        key = self.request.get('key')
        epub = db.get(key)
        template_values = {
            "current_user" : get_current_session().get("account"),
            "title" : epub.blob.filename,
            "key" : key,
            "files" : epub.internals(),
            "use_name" : False
        }
        path = os.path.join(os.path.dirname(__file__), 'html/contents.html')
        self.response.out.write(template.render(path, template_values))

class Contents(blobstore_handlers.BlobstoreDownloadHandler):
    def get(self):
        components = self.request.path.split("/")
        id = urllib.unquote_plus(components[2])
        epub = model.ePubFile.get_by_id(long(id))
        template_values = {
            "current_user" : get_current_session().get("account"),
            "id" : id,
            "key" : epub.key(),
            "title" : epub.title,
            "cover_path" : epub.cover_path,
            "files" : epub.internals(only_chapters = True),
            "use_name" : True
        }
        path = os.path.join(os.path.dirname(__file__), 'html/contents.html')
        self.response.out.write(template.render(path, template_values))

class Download(blobstore_handlers.BlobstoreDownloadHandler):
    def get(self):
        key = self.request.get('key')
        blob_info = blobstore.BlobInfo.get(key)
        self.send_blob(blob_info, save_as = True)

class View(webapp.RequestHandler):
    def get(self):
        components = self.request.path.split("/")
        if len(components)>1:
            id = components[2]
        epub = model.ePubFile.get_by_id(long(id))
        if len(components)<4:
            self.redirect("/book/"+id)
            return
        path = urllib.unquote_plus("/".join(components[3:]))
        internal = epub.internals().filter("path = ",path).get()
        renderer = unpack.Unpacker()
        self.response.headers['Content-Type'] = renderer.contentHeader(internal)
        self.response.out.write(renderer.content(internal))

class Search(webapp.RequestHandler):
    def get(self):
        return self.post()

    def post(self):
        query_string = "owners:%s AND (name:%s OR html:%s)" % (get_current_session().get("account"), self.request.get('q'), self.request.get('q'))
        book_filter = self.request.get('book_filter')
        if book_filter is not None and len(book_filter.strip())>0:
            query_string = "book:%s AND %s" % (book_filter, query_string)
        logging.info("Search query "+query_string)
        sort_opts = search.SortOptions(match_scorer=search.MatchScorer())
        opts = search.QueryOptions(limit = 100, snippeted_fields = ['content'], sort_options = sort_opts)
        query = search.Query(query_string = query_string, options=opts)
        try:
            results = []
            for indexName in ["private", "public"]:
                index_results = []
                index = search.Index(indexName)
                search_results = index.search(query)
                for doc in search_results:
                    internal = db.get(doc.doc_id)
                    if internal is not None:
                        index_results.append({ "doc" : doc, "internal" : internal })
                results.append({'count' : search_results.number_found, 'results' : index_results})

            template_values = {
                "current_user" : get_current_session().get("account"),
                "private_results" : results[0]['results'],
                "private_count" : results[0]['count'],
                "public_results" : results[1]['results'],
                "public_count" : results[1]['count']
            }
            path = os.path.join(os.path.dirname(__file__), 'html/search_results.html')
            self.response.out.write(template.render(path, template_values))
        except search.Error:
            respondWithMessage(self, "Search error")

class Share(webapp.RequestHandler):
    def post(self):
        quote = model.Quote(
            epub = model.ePubFile.get_by_id(long(self.request.get('epub'))),
            file = db.get(self.request.get('file')),
            html = db.Text(self.request.get('html')),
            user = get_current_session().get("account")
        )
        quote.put()
        unpacker = unpack.Unpacker()
        unpacker.index_quote(quote)
        self.response.headers['Content-Type'] = 'application/json'
        self.response.out.write('{"result":"ok","url":"/quote/%s"}' % quote.key().id())

class Quotes(webapp.RequestHandler):
    def get(self):
        user = get_current_session().get("account")
        quotes = model.Quote.all().filter("user = ", user).order("epub")
        results = []
        for quote in quotes:
            html = re.sub('<[^<]+?>', '', quote.html)
            words = html.split(" ")
            words = words[0:6] if len(words) > 7 else words
            text = '"'+" ".join(words)+'"'
            results.append({ "title" : quote.epub.title, "key" : quote.key(), "text" : text})
        template_values = {
            "current_user" : get_current_session().get("account"),
            "quotes" : results
        }
        path = os.path.join(os.path.dirname(__file__), 'html/quotes.html')
        self.response.out.write(template.render(path, template_values))

class Quote(webapp.RequestHandler):
    def get(self):
        components = self.request.path.split("/")
        id = urllib.unquote_plus(components[2])
        quote = model.Quote.get_by_id(long(id))
        template_values = {
            "current_user" : get_current_session().get("account"),
            "quote" : quote
        }
        path = os.path.join(os.path.dirname(__file__), 'html/quote.html')
        self.response.out.write(template.render(path, template_values))


class Edit(webapp.RequestHandler):
    def get(self):
        components = self.request.path.split("/")
        id = urllib.unquote_plus(components[2])
        epub = model.ePubFile.get_by_id(long(id))
        template_values = {
            "current_user" : get_current_session().get("account"),
            "admin" : users.is_current_user_admin(),
            "edit" : epub.entry_count()<=1,
            "epub" : epub,
            "pr_license" : " selected" if epub.license=="Private" else "",
            "pd_license" : " selected" if epub.license=="Public Domain" else "",
            "cc_license" : " selected" if epub.license=="Creative Commons" else "",
        }
        path = os.path.join(os.path.dirname(__file__), 'html/metadata.html')
        self.response.out.write(template.render(path, template_values))

    def post(self):
        enforce_login(self)
        key = self.request.get('epub_key')
        epub = db.get(key)
        if not users.is_current_user_admin() and epub.entry_count()>1:
            self.redirect("/")
        if self.request.get('license') is not None and not users.is_current_user_admin():
            self.redirect("/")
        epub.language = self.request.get('language')
        epub.title = self.request.get('title')
        epub.creator = self.request.get('creator')
        epub.publisher = self.request.get('publisher')
        epub.rights = self.request.get('rights')
        epub.contributor = self.request.get('contributor')
        epub.identifier = self.request.get('identifier')
        epub.description = self.request.get('description')
        epub.date = self.request.get('date')
        epub.license = self.request.get('license')
        epub.put()
        self.redirect("/book/"+str(epub.key.id()))

class Account(webapp.RequestHandler):
    def get(self):
        enforce_login(self)
        session = get_current_session()
        account_key = session.get("account")
        account = None if account_key is None else db.get(account_key)
        template_values = {
            "current_user" : get_current_session().get("account"),
            "account" : account,
            "fbName" : "n/a" if account.facebookInfo is None else json.loads(account.facebookInfo)["name"]
        }
        path = os.path.join(os.path.dirname(__file__), 'html/account.html')
        self.response.out.write(template.render(path, template_values))

    def post(self):
        enforce_login(self)
        self.response.out.write("Change account")

class Request(webapp.RequestHandler):
    def get(self):
        enforce_login(self)
        key = self.request.get('key')
        template_values = {
            "current_user" : get_current_session().get("account"),
            "epub_key" : key,
        }
        path = os.path.join(os.path.dirname(__file__), 'html/request.html')
        self.response.out.write(template.render(path, template_values))

    def post(self):
        enforce_login(self)
        epub_key = self.request.get('epub_key')
        public_request = model.PublicRequest(
            epub = db.get(epub_key),
            user = db.get(get_current_session().get("account")),
        )
        public_request.supporting_data = self.request.get('support').replace("\n","<br/>")
        public_request.put()
        respondWithMessage(self, "Thank you! We have received your request.")

class Delete(webapp.RequestHandler):
    def get(self):
        confirm = self.request.get('confirm')
        if confirm!="true":
            return
        epub_key = self.request.get('key')
        epub = db.get(epub_key)
        account = get_current_session().get("account")
        entry = model.LibraryEntry.all().filter("epub = ",epub).filter("user =",db.get(account)).get()
        if entry is not None:
            db.delete(entry)
            if epub.entry_count()==0:
                for indexName in ["private","public"]:
                    index = search.Index(indexName)
                    opts = search.QueryOptions(limit = 1000, ids_only = True)
                    query = search.Query(query_string = "book:%s" % epub_key, options=opts)
                    docs = index.search(query)
                    for doc in docs:
                        index.remove(doc.doc_id)
                blobstore.delete(epub.blob.key())
                db.delete(epub)

            self.redirect('/list')
        else:
            self.response.out.write("Not permitted")

class DeleteQuote(webapp.RequestHandler):
    def get(self):
        confirm = self.request.get('confirm')
        if confirm!="true":
            return
        quote_key = self.request.get('key')
        quote = db.get(quote_key)
        account = get_current_session().get("account")
        if quote.user.key() == account:
            db.delete(quote)
            search.Index("quotes").remove(quote_key)
            self.redirect('/quotes')
        else:
            self.response.out.write("Not permitted")

class Clear(webapp.RequestHandler):
    def get(self):
        if not users.is_current_user_admin():
            self.response.out.write("No")
            return
        for indexName in ["private","public"]:
            index = search.Index(indexName)
            for doc in index.list_documents(limit=1000, ids_only=True):
                index.remove(doc.doc_id)

app = webapp.WSGIApplication([
    ('/', Main),
    ('/about', About),
    ('/logout', LogOut),
    ('/upload', UploadForm),
    ('/upload_complete', UploadHandler),
    ('/index', Index),
    ('/books', List),
    ('/list', List),
    ('/unpack_internal', UnpackInternal),
    ('/view/.*', View),
    ('/book/.*', Contents),
    ('/manifest', Manifest),
    ('/download', Download),
    ('/search', Search),
    ('/request', Request),
    ('/share', Share),
    ('/quote/.*', Quote),
    ('/quotes', Quotes),
    ('/edit/.*', Edit),
    ('/account', Account),
    ('/delete', Delete),
    ('/delete_quote', DeleteQuote),
    ('/clearindexes', Clear),
    ],
    debug=True)
