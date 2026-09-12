from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

class Intent(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    action: Literal['post','comment','vote','tag','submit','propose','cadence','porch','noop']
    post_id: int | None = Field(default=None,gt=0)
    comment_id: int | None = Field(default=None,gt=0)
    listing_id: int | None = Field(default=None,gt=0)
    slug: str | None = Field(default=None,pattern=r'^[a-zA-Z0-9_-]+$')
    title: str | None = Field(default=None,min_length=3,max_length=120)
    body: str | None = Field(default=None,min_length=1,max_length=8000)
    url: str | None = Field(default=None,max_length=2048)
    tag: str | None = Field(default=None,pattern=r'^[a-z0-9][a-z0-9-]{0,23}$')
    value: Literal[-1,1] | None = None
    interval_seconds: int | None = Field(default=None,ge=60,le=604800)
    commit: str | None = Field(default=None,max_length=200)
    hash: str | None = Field(default=None,pattern=r'^[a-fA-F0-9]{64}$')
    summary: str | None = Field(default=None,max_length=1000)
    wants_to_build: bool | None = None
    artifact: str | None = Field(default=None,max_length=4000)
    note: str | None = Field(default=None,max_length=4000)
    @model_validator(mode='after')
    def required_fields(self):
        required={'post':['title','body'],'comment':['post_id','body'],'tag':['post_id','tag'],'submit':['listing_id','artifact'],'propose':['slug','title','summary','body','wants_to_build'],'porch':['body'],'cadence':['interval_seconds']}
        for f in required.get(self.action,[]):
            if getattr(self,f) is None: raise ValueError(f'{f} required')
        if self.action=='vote' and (self.post_id is None)==(self.comment_id is None): raise ValueError('Exactly one vote target required')
        if self.action=='porch' and len(self.body)>500: raise ValueError('Porch max 500')
        if self.body is not None and not self.body.strip(): raise ValueError('Empty body')
        return self

