## Usage:

First, make sure you've installed asset_bender: https://github.com/HubSpot/asset_bender/tree/master

Secondly, make sure that `PROJ_NAME`, `PROJ_DIR`, `BENDER_S3_DOMAIN`, and `BENDER_CDN_DOMAIN` are set in your
settings. `PROJ_NAME` should match the name in static_conf.json and `PROJ_DIR` needs to point to the python
module path (via something like `PROJ_DIR = dirname(realpath(__file__))`). 

`BENDER_S3_DOMAIN` is the domain that points to your S3 bucket and `BENDER_CDN_DOMAIN` is the CDN domain in
front of S3 (if you have one).

Thirdly, make sure that you've included these lines in your Manifest.in:

    global-include static_conf.json
    global-include prebuilt_recursive_static_conf.json


Next, in your app's context processor do:

```python
from django.template import RequestContext
from asset_bender.bundling import BenderAssets

def my_context_processor(request):
    context = RequestContext(request)

    bender_assets = BenderAssets([
         'my_project/static/js/my_project_bundle.js', 
         'my_project/static/css/my_project_bundle.css', 

         'some_library/static/js/some_library_bundle.js', 
         'some_library/static/css/some_library_bundle.js'

         ... etc ...
     ], request.GET)

     context.update(bender_assets.generate_context_dict())
     return context
```

And lastly, in your base template you'll need to include these templates:


```html
<head>
    ...
    {% include "asset_bender/scaffold/head.html" %}
    ...
</head>

<body>
    ...
    {% include "asset_bender/scaffold/end_of_body.html" %}
</body>
```


To manually include a particular static asset in your HTML, use the template tag:
    
```
{% load asset_bender_tags %}
{% bender_url "project_name/static/js/my-file.js" %}
```

The tag will output a full url with the proper domain and version number (as specified by this projects's dependencies).